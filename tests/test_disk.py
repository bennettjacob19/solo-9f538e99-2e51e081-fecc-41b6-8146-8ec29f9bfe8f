"""端到端自测：纯标准库 unittest，不依赖 Flask。

覆盖：
1. 预置镜像布局（空洞 / 写过的零块 / 非零块）与实占统计
2. 完整回收：只打零块、容量不变、读值不变、实占下降
3. 并发写入竞争：判零之后写入非零数据，回收不得抹掉新数据
4. 中断重跑：中断后原盘与已打的洞保留，重启可重新发起并继续回收
5. API 层输入校验与路由行为（stdlib WSGI 入口）
6. 坏块：注入读故障（重启仍在）、原写入入口修复（备用槽接替、
   内容与映射落盘才算成功）、无空槽写入失败且坏块状态保留
7. 并发修复：两坏块争最后一槽最多一个成功；槽等待写盘时其他块
   照常读写、有空槽可修复另一坏块
8. 修复失败 / 进程中断：未提交修复的块仍报读错、预留槽可再次分配；
   重启沿用已提交的映射
9. 回收跳过故障原位置；备用槽全零块打洞后槽仍归原块号
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from app.api import ApiCore, ApiError
from app.constants import BLOCK_SIZE, STATE_DATA, STATE_FAULTY, STATE_HOLE, STATE_ZERO
from app.disk import (
    AlreadyRemappedError,
    DiskFaultError,
    DiskImage,
    NoSpareSlotError,
    create_image,
)
from app.reclaim import ReclaimManager
from app.server_stdlib import create_wsgi_app


class TempImageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reclaim-test-")
        self.path = os.path.join(self.tmp, "disk.raw")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open_disk(self, blocks=32):
        return create_image(self.path, blocks)


class TestPresetImage(TempImageTestCase):
    def test_layout_and_actual_bytes(self):
        disk = self.open_disk(32)  # quarter=8
        try:
            self.assertEqual(disk.capacity, 32 * BLOCK_SIZE)
            self.assertEqual(disk.block_state(0), STATE_HOLE)      # 0..7 空洞
            self.assertEqual(disk.block_state(8), STATE_ZERO)      # 8..15 写过的零块
            self.assertEqual(disk.block_state(16), STATE_DATA)     # 16..23 非零
            self.assertEqual(disk.block_state(24), STATE_HOLE)     # 24..31 空洞

            # 已分配的是 8 个零块 + 8 个非零块 = 16 * 4KiB（备用槽为空洞不占空间）
            self.assertEqual(disk.actual_bytes(), 16 * BLOCK_SIZE)
            self.assertEqual(os.path.getsize(self.path), disk.physical_size)
        finally:
            disk.close()

    def test_reopen_is_idempotent(self):
        d1 = self.open_disk(16)
        d1.close()
        before = os.path.getsize(self.path)
        d2 = self.open_disk(16)
        try:
            self.assertEqual(os.path.getsize(self.path), before)
            self.assertEqual(d2.block_state(4), STATE_ZERO)
        finally:
            d2.close()


class TestFullReclaim(TempImageTestCase):
    def test_reclaim_punches_only_zero_blocks(self):
        disk = self.open_disk(32)
        try:
            zero_indices = [i for i in range(32) if disk.block_state(i) == STATE_ZERO]
            data_indices = [i for i in range(32) if disk.block_state(i) == STATE_DATA]
            self.assertEqual(len(zero_indices), 8)
            before = disk.actual_bytes()
            zero_values = {i: disk.read_block(i) for i in zero_indices}
            data_values = {i: disk.read_block(i) for i in data_indices}

            events = list(disk.reclaim())
            punched = sorted(e["index"] for e in events if e["state"] == "punched")
            self.assertEqual(punched, sorted(zero_indices))

            # 容量不变、实占下降：8 个零块全部释放
            self.assertEqual(os.path.getsize(self.path), disk.physical_size)
            self.assertEqual(disk.actual_bytes(), before - 8 * BLOCK_SIZE)

            # 打洞后块读值不变（零块读回仍全零，非零块原样）
            for i in zero_indices:
                self.assertEqual(disk.read_block(i), zero_values[i])
                self.assertEqual(disk.read_block(i), b"\x00" * BLOCK_SIZE)
                self.assertEqual(disk.block_state(i), STATE_HOLE)
            for i in data_indices:
                self.assertEqual(disk.read_block(i), data_values[i])
                self.assertEqual(disk.block_state(i), STATE_DATA)

            # 再跑一轮：没有可回收的块，实占不再变化
            second = list(disk.reclaim())
            self.assertTrue(all(e["state"] != "punched" for e in second))
            self.assertEqual(disk.actual_bytes(), before - 8 * BLOCK_SIZE)
        finally:
            disk.close()

    def test_writing_zero_allocates_host_block(self):
        disk = self.open_disk(16)
        try:
            hole_index = 0
            self.assertEqual(disk.block_state(hole_index), STATE_HOLE)
            disk.write_block(hole_index, b"\x00" * BLOCK_SIZE)
            self.assertEqual(disk.block_state(hole_index), STATE_ZERO)
            # 回收后该块重新变空洞，读回仍为零
            list(disk.reclaim())
            self.assertEqual(disk.block_state(hole_index), STATE_HOLE)
            self.assertEqual(disk.read_block(hole_index), b"\x00" * BLOCK_SIZE)
        finally:
            disk.close()


class TestConcurrentWrite(TempImageTestCase):
    def test_nonzero_write_after_zero_judgement_is_never_punched(self):
        """关键竞争：扫描已判定为零块后，该块在打洞前被写入非零数据。

        块锁保证“判零 + 打洞”与 write_block 互斥。本测试确定性复现：
        回收线程打完前一个零块、即将处理目标块时，写线程持有目标块锁
        完成非零写入；回收线程拿到锁后二次校验读到非零，必须跳过打洞，
        新数据原样保留。
        """
        disk = self.open_disk(32)
        try:
            target = 9  # 预置零块；块 8 是它前面的第一个零块
            self.assertEqual(disk.block_state(target), STATE_ZERO)
            new_payload = (b"RACED-NONZERO-DATA" + b"\x11" * BLOCK_SIZE)[:BLOCK_SIZE]

            first_block_done = threading.Event()
            events = []

            def worker():
                for event in disk.reclaim(
                    on_event=lambda ev: (
                        events.append(ev),
                        first_block_done.set()
                        if ev["index"] == 8 and ev["state"] == "punched"
                        else None,
                    )
                ):
                    pass

            t = threading.Thread(target=worker)
            target_lock = disk._block_locks[target]
            # 先持有目标块锁，再启动回收，回收处理到目标块时会阻塞
            with target_lock:
                t.start()
                self.assertTrue(first_block_done.wait(5))
                # 持锁写入（等价于 write_block 已获得块锁后的写动作）
                os.pwrite(disk._fd, new_payload, target * BLOCK_SIZE)
            t.join(timeout=5)
            self.assertFalse(t.is_alive())

            target_events = [e for e in events if e["index"] == target]
            self.assertEqual(len(target_events), 1)
            # 二次校验读到非零数据 → 跳过打洞，而不是抹掉新数据
            self.assertEqual(target_events[0]["state"], STATE_DATA)
            self.assertEqual(disk.read_block(target), new_payload)
            self.assertEqual(disk.block_state(target), STATE_DATA)

            # 其它零块不受影响，照常被打洞（8 个零块中除 target 外的 7 个）
            punched = sorted(e["index"] for e in events if e["state"] == "punched")
            self.assertNotIn(target, punched)
            self.assertEqual(len(punched), 7)
        finally:
            disk.close()

    def test_concurrent_writes_during_full_reclaim(self):
        """高压并发：回收进行中多个线程持续随机写，结束后盘上数据与最后写入一致。"""
        import random

        disk = self.open_disk(64)
        rng = random.Random(20261122)
        stop = threading.Event()
        errors = []

        def writer(seed):
            local = random.Random(seed)
            payloads = {}
            while not stop.is_set():
                idx = local.randrange(0, 64)
                kind = local.choice(["zero", "data"])
                if kind == "zero":
                    payload = b"\x00" * BLOCK_SIZE
                else:
                    payload = (f"w{seed}-b{idx}".encode() + b"\x77" * BLOCK_SIZE)[:BLOCK_SIZE]
                try:
                    disk.write_block(idx, payload)
                    payloads[idx] = payload
                except Exception as exc:  # pragma: no cover - 失败即测试失败
                    errors.append(exc)
            return payloads

        threads = [threading.Thread(target=writer, args=(s,)) for s in range(4)]
        # 线程内部各自记录最后写入，回收结束后逐块核对“最后写者”的值
        last_writes = {}
        originals = {i: disk.read_block(i) for i in range(64)}

        # 用包装写入记录全局最后值
        real_write = disk.write_block

        def recorded_write(index, data):
            last_writes[index] = bytes(data)
            return real_write(index, data)

        disk.write_block = recorded_write
        for t in threads:
            t.start()
        manager = ReclaimManager(disk)
        manager.start()
        # 等回收完成（写入仍在继续，可能出现新零块，下一轮可回收）
        while manager.state()["running"]:
            threading.Event().wait(0.005)
        stop.set()
        for t in threads:
            t.join()
        disk.write_block = real_write
        self.assertEqual(errors, [])

        for i in range(64):
            expected = last_writes.get(i, originals[i])
            self.assertEqual(disk.read_block(i), expected, f"块 {i} 数据与最后写入不一致")

        # 再跑一轮回收（无写入），应正常完成且不改变任何读值
        before = {i: disk.read_block(i) for i in range(64)}
        list(disk.reclaim())
        for i in range(64):
            self.assertEqual(disk.read_block(i), before[i])
    def test_hole_is_skipped_even_when_writer_creates_then_punches(self):
        """已有空洞在扫描中被 SEEK_DATA 整段跳过，不会触发无谓处理。"""
        disk = self.open_disk(16)
        try:
            events = list(disk.reclaim())
            touched = {e["index"] for e in events}
            # 预置空洞块（0..3、12..15）不应出现在事件里
            for hole in (0, 1, 2, 3, 12, 13, 14, 15):
                self.assertNotIn(hole, touched)
        finally:
            disk.close()


class TestInterruptionAndRestart(TempImageTestCase):
    def test_cancel_keeps_disk_and_holes_then_resume(self):
        disk = self.open_disk(256)  # 与生产默认一致：64 个零块
        try:
            capacity = disk.capacity
            zero_before = [i for i in range(256) if disk.block_state(i) == STATE_ZERO]
            self.assertEqual(len(zero_before), 64)

            # 注入每块处理延迟，制造稳定的中断窗口（不改变任何盘上语义）
            import time
            real_on = None

            def slow_reclaim(should_stop=None, on_event=None):
                def wrapped(ev):
                    time.sleep(0.002)
                    if on_event:
                        on_event(ev)
                yield from DiskImage.reclaim(disk, should_stop, wrapped)

            disk.reclaim = slow_reclaim
            manager = ReclaimManager(disk)
            manager.start()

            # 等至少打一个洞后请求中断
            state = manager.state()
            while state["running"] and state["punched_blocks"] == 0:
                threading.Event().wait(0.005)
                state = manager.state()
            manager.cancel()
            while manager.state()["running"]:
                threading.Event().wait(0.005)

            final = manager.state()
            self.assertTrue(final["finished"])
            self.assertTrue(final["cancel_requested"])
            self.assertGreaterEqual(final["punched_blocks"], 1)
            self.assertLess(final["punched_blocks"], len(zero_before))
            actual_after_cancel = disk.actual_bytes()

            # 已打的洞真实落盘：重开进程视角（新 DiskImage 实例）仍保留
            disk.close()
            disk2 = DiskImage(self.path, 256)
            holes_after = [i for i in range(256) if disk2.block_state(i) == STATE_HOLE]
            self.assertGreaterEqual(len(holes_after), 64 + final["punched_blocks"])
            self.assertEqual(os.path.getsize(self.path), disk2.physical_size)

            # 重启后重新发起回收：跳过已有空洞，把剩余零块收完
            manager2 = ReclaimManager(disk2)
            manager2.start()
            while manager2.state()["running"]:
                threading.Event().wait(0.005)
            st2 = manager2.state()
            self.assertIsNone(st2["error"])
            remaining_zeros = [i for i in range(256) if disk2.block_state(i) == STATE_ZERO]
            self.assertEqual(remaining_zeros, [])
            # 两轮合计释放全部 64 个零块，只剩 64 个非零块占用空间
            self.assertEqual(disk2.actual_bytes(), 64 * BLOCK_SIZE)
            self.assertEqual(
                actual_after_cancel,
                (64 + 64 - final["punched_blocks"]) * BLOCK_SIZE,
            )
            disk2.close()
            disk = None  # 防止 tearDown 重复关闭
        finally:
            if disk is not None:
                disk.close()

    def test_restart_with_existing_fully_reclaimed_image(self):
        disk = self.open_disk(16)
        list(disk.reclaim())
        disk.close()
        # 模拟进程重启：镜像复用，重新回收为空操作
        again = create_image(self.path, 16)
        try:
            manager = ReclaimManager(again)
            manager.start()
            st = manager.state()
            while st["running"]:
                threading.Event().wait(0.005)
                st = manager.state()
            self.assertIsNone(st["error"])
            self.assertEqual(st["punched_blocks"], 0)
            # 只剩非零块占用空间
            self.assertEqual(again.actual_bytes(), 4 * BLOCK_SIZE)
        finally:
            again.close()


def _payload(tag: bytes, fill: bytes = b"\x55") -> bytes:
    return (tag + fill * BLOCK_SIZE)[:BLOCK_SIZE]


class TestFaultInjection(TempImageTestCase):
    def test_fault_read_error_and_restart_persistence(self):
        disk = self.open_disk(32)
        try:
            disk.inject_fault(20)  # 非零数据块
            with self.assertRaises(DiskFaultError):
                disk.read_block(20)
            self.assertEqual(disk.block_state(20), STATE_FAULTY)
            # 幂等：重复注入不报错
            disk.inject_fault(20)
            # 其他块不受影响
            self.assertEqual(disk.read_block(21)[:9], b"block-002")
        finally:
            disk.close()
        # 重启后故障仍在
        disk2 = DiskImage(self.path, 32)
        try:
            with self.assertRaises(DiskFaultError):
                disk2.read_block(20)
            self.assertEqual(disk2.remap_status()["unrepaired"], [20])
        finally:
            disk2.close()

    def test_inject_on_remapped_block_rejected(self):
        disk = self.open_disk(32)
        try:
            disk.inject_fault(20)
            disk.write_block(20, _payload(b"FIXED"))
            with self.assertRaises(AlreadyRemappedError):
                disk.inject_fault(20)
        finally:
            disk.close()


class TestRepair(TempImageTestCase):
    def test_repair_via_original_write_entry_and_restart(self):
        disk = self.open_disk(32)
        new_value = _payload(b"REPAIRED-BLOCK-20")
        try:
            disk.inject_fault(20)
            result = disk.write_block(20, new_value)  # 原写入入口触发修复
            self.assertTrue(result["repaired"])
            self.assertEqual(result["slot"], 0)
            # 此后按原块号读写，透明落到备用槽
            self.assertEqual(disk.read_block(20), new_value)
            self.assertEqual(disk.block_state(20), STATE_DATA)
            status = disk.remap_status()
            self.assertEqual(status["mapping"], {"20": 0})
            self.assertEqual(status["spare_free"], 1)
            # 再写仍走备用槽
            newer = _payload(b"SECOND-WRITE")
            result2 = disk.write_block(20, newer)
            self.assertFalse(result2["repaired"])
            self.assertEqual(disk.read_block(20), newer)
        finally:
            disk.close()
        # 重启沿用已提交的映射；故障标记也保留（原位置仍被回收跳过）
        disk2 = DiskImage(self.path, 32)
        try:
            self.assertEqual(disk2.read_block(20), newer)
            st = disk2.remap_status()
            self.assertEqual(st["mapping"], {"20": 0})
            self.assertEqual(st["faulty"], [20])
            self.assertEqual(st["unrepaired"], [])
            self.assertEqual(st["spare_free"], 1)
        finally:
            disk2.close()

    def test_repair_failure_releases_slot_and_keeps_fault(self):
        disk = self.open_disk(32)
        try:
            disk.inject_fault(20)
            disk._slot_write_hook = lambda slot: (_ for _ in ()).throw(
                RuntimeError("模拟写盘失败")
            )
            with self.assertRaises(RuntimeError):
                disk.write_block(20, _payload(b"WONT-COMMIT"))
            # 坏块状态保留，预留槽已释放
            self.assertEqual(disk.block_state(20), STATE_FAULTY)
            with self.assertRaises(DiskFaultError):
                disk.read_block(20)
            self.assertEqual(disk.remap_status()["spare_free"], 2)
            # 解除故障注入后重试成功，拿到同一个槽
            disk._slot_write_hook = None
            result = disk.write_block(20, _payload(b"RETRY-OK"))
            self.assertTrue(result["repaired"])
            self.assertEqual(result["slot"], 0)
            self.assertEqual(disk.read_block(20), _payload(b"RETRY-OK"))
        finally:
            disk.close()

    def test_commit_persist_failure_keeps_fault_and_frees_slot(self):
        """映射落盘失败：内存态一并回滚，块仍报读错，预留槽可再次分配。"""
        disk = self.open_disk(32)
        try:
            disk.inject_fault(20)
            orig_persist = disk._remap._persist_locked
            disk._remap._persist_locked = lambda: (_ for _ in ()).throw(
                OSError("模拟映射落盘失败")
            )
            with self.assertRaises(OSError):
                disk.write_block(20, _payload(b"NO-COMMIT"))
            # 内存态也回滚：块仍故障未修复，槽已释放
            self.assertEqual(disk.block_state(20), STATE_FAULTY)
            with self.assertRaises(DiskFaultError):
                disk.read_block(20)
            st = disk.remap_status()
            self.assertEqual(st["spare_free"], 2)
            self.assertEqual(st["mapping"], {})
            # 恢复落盘后重试成功
            disk._remap._persist_locked = orig_persist
            result = disk.write_block(20, _payload(b"OK-AGAIN"))
            self.assertTrue(result["repaired"])
            self.assertEqual(disk.read_block(20), _payload(b"OK-AGAIN"))
        finally:
            disk.close()

    def test_no_spare_slot_write_fails_and_fault_kept(self):
        disk = self.open_disk(32)
        try:
            for b in (20, 21, 22):
                disk.inject_fault(b)
            disk.write_block(20, _payload(b"A"))
            disk.write_block(21, _payload(b"B"))
            # 两个备用槽已用完：第三个坏块写入失败，坏块状态保留
            with self.assertRaises(NoSpareSlotError):
                disk.write_block(22, _payload(b"C"))
            self.assertEqual(disk.block_state(22), STATE_FAULTY)
            with self.assertRaises(DiskFaultError):
                disk.read_block(22)
            st = disk.remap_status()
            self.assertEqual(st["spare_free"], 0)
            self.assertEqual(st["unrepaired"], [22])
        finally:
            disk.close()
        # 重启后依旧：无空槽、坏块 22 仍报读错
        disk2 = DiskImage(self.path, 32)
        try:
            with self.assertRaises(NoSpareSlotError):
                disk2.write_block(22, _payload(b"C2"))
            with self.assertRaises(DiskFaultError):
                disk2.read_block(22)
        finally:
            disk2.close()


class TestConcurrentRepair(TempImageTestCase):
    def test_two_bad_blocks_race_for_last_slot(self):
        """两个坏块争最后一个备用槽：最多一个修复成功。"""
        disk = self.open_disk(32)
        try:
            disk.inject_fault(20)
            disk.write_block(20, _payload(b"FIRST"))  # 占掉槽 0
            disk.inject_fault(21)
            disk.inject_fault(22)

            barrier = threading.Barrier(2)
            results, errors = {}, {}

            def repair(b):
                barrier.wait()
                try:
                    results[b] = disk.write_block(b, _payload(b"RACE-%d" % b))
                except NoSpareSlotError as exc:
                    errors[b] = exc

            threads = [threading.Thread(target=repair, args=(b,)) for b in (21, 22)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
                self.assertFalse(t.is_alive())

            # 恰好一个成功、一个因无空槽失败
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            winner = next(iter(results))
            loser = next(iter(errors))
            self.assertEqual(results[winner]["slot"], 1)
            self.assertEqual(disk.block_state(loser), STATE_FAULTY)
            with self.assertRaises(DiskFaultError):
                disk.read_block(loser)
            st = disk.remap_status()
            self.assertEqual(st["spare_free"], 0)
            self.assertEqual(st["unrepaired"], [loser])
        finally:
            disk.close()

    def test_others_unblocked_while_slot_write_in_flight(self):
        """某槽等待写盘时：其他块照常读写，有空槽也能修复另一坏块。"""
        disk = self.open_disk(32)
        try:
            for b in (20, 21, 22):
                disk.inject_fault(b)

            hook_entered = threading.Event()
            release_hook = threading.Event()
            first_call = threading.Event()

            def hook(slot):
                if not first_call.is_set():  # 只拖住第一个修复
                    first_call.set()
                    hook_entered.set()
                    self.assertTrue(release_hook.wait(5))

            disk._slot_write_hook = hook
            outcome = {}

            def repair_a():
                outcome["a"] = disk.write_block(20, _payload(b"SLOW-A"))

            t = threading.Thread(target=repair_a)
            t.start()
            self.assertTrue(hook_entered.wait(5))  # A 已占槽、正在等写盘

            # 其他块照常读写（不被在途修复阻塞）
            disk.write_block(10, _payload(b"HEALTHY"))
            self.assertEqual(disk.read_block(10), _payload(b"HEALTHY"))
            self.assertEqual(disk.read_block(0), b"\x00" * BLOCK_SIZE)

            # 有空槽：另一坏块照常修复成功
            res_b = disk.write_block(21, _payload(b"FAST-B"))
            self.assertTrue(res_b["repaired"])
            self.assertEqual(res_b["slot"], 1)

            # 此时两槽俱占：第三个坏块修复失败
            with self.assertRaises(NoSpareSlotError):
                disk.write_block(22, _payload(b"NO-SLOT"))

            release_hook.set()
            t.join(timeout=10)
            self.assertFalse(t.is_alive())
            self.assertTrue(outcome["a"]["repaired"])
            self.assertEqual(outcome["a"]["slot"], 0)

            self.assertEqual(disk.read_block(20), _payload(b"SLOW-A"))
            self.assertEqual(disk.read_block(21), _payload(b"FAST-B"))
            self.assertEqual(disk.block_state(22), STATE_FAULTY)
            st = disk.remap_status()
            self.assertEqual(st["mapping"], {"20": 0, "21": 1})
            self.assertEqual(st["spare_free"], 0)
        finally:
            disk.close()


class TestRepairCrashRestart(TempImageTestCase):
    def test_crash_before_commit_keeps_fault_and_frees_slot(self):
        """进程在内容落盘后、映射提交前被杀：块仍报读错，槽可再次分配。"""
        marker = os.path.join(self.tmp, "hook-ready")
        payload_a = _payload(b"COMMITTED-A")
        payload_b = _payload(b"UNCOMMITTED-B")
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        child_code = f"""
import sys, time
sys.path.insert(0, {repo_root!r})
from app.disk import create_image
disk = create_image({self.path!r}, 32)
disk.inject_fault(20)
disk.inject_fault(21)
disk.write_block(20, {payload_a!r})  # 已提交：槽 0
def hook(slot):
    with open({marker!r}, "w") as fh:
        fh.write("ready")
    time.sleep(60)  # 在映射提交前一直挂起，等待被杀
disk._slot_write_hook = hook
disk.write_block(21, {payload_b!r})  # 内容落盘后、提交前被 SIGKILL
"""
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", child_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # 等子进程把块 21 的内容写入备用槽并挂起在提交前
            for _ in range(200):
                if os.path.exists(marker) or proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(os.path.exists(marker), "子进程未进入提交前挂起点")
            proc.kill()  # 模拟进程中断（SIGKILL）
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

        # 重启：已提交映射沿用，未提交的块仍报读错，预留槽可再次分配
        disk = DiskImage(self.path, 32)
        try:
            self.assertEqual(disk.read_block(20), payload_a)  # 已提交映射生效
            with self.assertRaises(DiskFaultError):
                disk.read_block(21)  # 未提交修复：仍报读错
            st = disk.remap_status()
            self.assertEqual(st["mapping"], {"20": 0})
            self.assertEqual(st["spare_free"], 1)  # 槽 1 未被占用
            # 槽 1 可再次分配：重写块 21 修复成功
            payload_b2 = _payload(b"RETRY-B")
            result = disk.write_block(21, payload_b2)
            self.assertTrue(result["repaired"])
            self.assertEqual(result["slot"], 1)
            self.assertEqual(disk.read_block(21), payload_b2)
        finally:
            disk.close()


class TestReclaimWithFaults(TempImageTestCase):
    def test_reclaim_skips_faulty_and_punches_spare_slots(self):
        disk = self.open_disk(32)  # 零块 8..15，非零 16..23，备用槽 32..33
        try:
            disk.inject_fault(9)   # 故障零块：原位置不得被打洞
            disk.inject_fault(16)  # 故障数据块，修复为非零 → 槽 0
            disk.write_block(16, _payload(b"IN-SPARE-0"))
            disk.inject_fault(17)  # 故障数据块，修复为全零 → 槽 1
            disk.write_block(17, b"\x00" * BLOCK_SIZE)
            before = disk.actual_bytes()

            events = list(disk.reclaim())
            by_index = {}
            for e in events:
                by_index.setdefault(e["index"], e)

            # 故障原位置被跳过：不读不打洞，空间保留
            self.assertEqual(by_index[9]["state"], STATE_FAULTY)
            self.assertEqual(by_index[16]["state"], STATE_FAULTY)
            self.assertEqual(by_index[17]["state"], STATE_FAULTY)
            self.assertEqual(disk.block_state(9), STATE_FAULTY)
            # 其他零块照常打洞
            self.assertEqual(by_index[8]["state"], "punched")
            # 备用槽：槽 0 非零保留，槽 1 全零被打洞
            slot_events = {e["slot"]: e for e in events if "slot" in e}
            self.assertEqual(slot_events[0]["state"], STATE_DATA)
            self.assertEqual(slot_events[1]["state"], "punched")
            # 打洞后槽仍归原块号，读回仍全零
            st = disk.remap_status()
            self.assertEqual(st["mapping"], {"16": 0, "17": 1})
            self.assertEqual(disk.read_block(17), b"\x00" * BLOCK_SIZE)
            self.assertEqual(disk.read_block(16), _payload(b"IN-SPARE-0"))
            # 实占下降包含槽 1 释放的一个块
            self.assertLessEqual(disk.actual_bytes(), before - BLOCK_SIZE)
            # 已修复块仍可写：写入已打洞的槽会重新分配
            disk.write_block(17, _payload(b"BACK-AGAIN"))
            self.assertEqual(disk.read_block(17), _payload(b"BACK-AGAIN"))
        finally:
            disk.close()


class TestApiCore(TempImageTestCase):
    def setUp(self):
        super().setUp()
        self.disk = self.open_disk(16)
        self.core = ApiCore(self.disk)

    def tearDown(self):
        self.disk.close()
        super().tearDown()

    def test_status_and_block_read(self):
        status = self.core.status()
        self.assertEqual(status["stats"]["block_count"], 16)
        self.assertEqual(len(status["blocks"]), 16)
        self.assertEqual(status["reclaim"]["running"], False)
        info = self.core.read_block(4)  # 16 块镜像：4..7 为写过的零块
        self.assertEqual(info["state"], STATE_ZERO)
        self.assertEqual(len(bytes.fromhex(info["hex"])), BLOCK_SIZE)

    def test_write_validation(self):
        with self.assertRaises(ApiError) as ctx:
            self.core.write_block(0, {"hex": "abcd"})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError):
            self.core.write_block(0, {"hex": "zz"})
        with self.assertRaises(ApiError) as ctx:
            self.core.write_block(99, {"hex": (b"\x00" * BLOCK_SIZE).hex()})
        self.assertEqual(ctx.exception.status, 404)

    def test_write_then_read_roundtrip_and_reclaim_via_api(self):
        payload = (b"API-WRITE" + b"\x33" * BLOCK_SIZE)[:BLOCK_SIZE]
        self.core.write_block(0, {"hex": payload.hex()})
        self.assertEqual(bytes.fromhex(self.core.get_block(0)["hex"]), payload)

        self.core.write_pattern(0, "zero")
        self.assertEqual(self.core.get_block(0)["state"], STATE_ZERO)

        before = self.core.status()["stats"]["actual_bytes"]
        self.core.reclaim_start()
        st = self.core.manager.state()
        while st["running"]:
            threading.Event().wait(0.005)
            st = self.core.manager.state()
        after = self.core.status()["stats"]["actual_bytes"]
        self.assertLess(after, before)
        self.assertEqual(self.core.get_block(0)["state"], STATE_HOLE)
        self.assertEqual(bytes.fromhex(self.core.get_block(0)["hex"]), b"\x00" * BLOCK_SIZE)

    def test_fault_and_repair_via_api(self):
        self.core.inject_fault(4)  # 16 块镜像：4..7 为写过的零块
        with self.assertRaises(ApiError) as ctx:
            self.core.read_block(4)
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("读故障", ctx.exception.message)

        payload = _payload(b"API-REPAIR")
        resp = self.core.write_block(4, {"hex": payload.hex()})
        self.assertTrue(resp["repaired"])
        self.assertEqual(resp["remapped_to"], 0)
        self.assertEqual(bytes.fromhex(self.core.get_block(4)["hex"]), payload)

        # 已修复块不在原位置，不能再注入故障
        with self.assertRaises(ApiError) as ctx:
            self.core.inject_fault(4)
        self.assertEqual(ctx.exception.status, 409)

        remap = self.core.status()["remap"]
        self.assertEqual(remap["mapping"], {"4": 0})
        self.assertEqual(remap["spare_free"], 1)
        self.assertEqual(remap["unrepaired"], [])

    def test_no_spare_slot_api_507(self):
        for b in (4, 5, 6):
            self.core.inject_fault(b)
        ok = {"hex": _payload(b"X").hex()}
        self.core.write_block(4, ok)
        self.core.write_block(5, ok)
        with self.assertRaises(ApiError) as ctx:
            self.core.write_block(6, ok)
        self.assertEqual(ctx.exception.status, 507)
        remap = self.core.status()["remap"]
        self.assertEqual(remap["spare_free"], 0)
        self.assertEqual(remap["unrepaired"], [6])

    def test_wsgi_routing_and_errors(self):
        app = create_wsgi_app(self.core)

        def call(method, path, body=None):
            req = {
                "REQUEST_METHOD": method,
                "PATH_INFO": path,
                "CONTENT_LENGTH": str(len(body)) if body else "0",
                "wsgi.input": io.BytesIO(body or b""),
            }
            captured = {}

            def start_response(status, headers):
                captured["status"] = status
                captured["headers"] = headers

            chunks = app(req, start_response)
            raw = b"".join(chunks)
            code = int(captured["status"].split(" ")[0])
            return code, json.loads(raw.decode())

        code, body = call("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(body["stats"]["block_count"], 16)

        code, body = call("GET", "/api/blocks/4")
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], STATE_ZERO)

        code, body = call("POST", "/api/blocks/1/write",
                          json.dumps({"hex": "ab"}).encode())
        self.assertEqual(code, 400)
        self.assertIn("4096", body["error"])

        code, body = call("GET", "/nope")
        self.assertEqual(code, 404)

        code, body = call("POST", "/api/blocks/2/write-pattern/zero")
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], STATE_ZERO)

        # 注入故障 → 读 500；修复写 → 200 且带重映射信息
        code, body = call("POST", "/api/blocks/3/fault")
        self.assertEqual(code, 200)
        self.assertTrue(body["faulty"])
        code, body = call("POST", "/api/blocks/3/read")
        self.assertEqual(code, 500)
        self.assertIn("读故障", body["error"])
        code, body = call(
            "POST", "/api/blocks/3/write",
            json.dumps({"hex": _payload(b"WSGI-FIX").hex()}).encode(),
        )
        self.assertEqual(code, 200)
        self.assertTrue(body["repaired"])
        self.assertEqual(body["remapped_to"], 0)
        code, body = call("GET", "/api/blocks/3")
        self.assertEqual(code, 200)
        self.assertEqual(bytes.fromhex(body["hex"]), _payload(b"WSGI-FIX"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
