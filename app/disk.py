"""raw 虚拟磁盘镜像：4 KiB 块读写、坏块故障与备用槽重映射、扫描打洞回收。

设计要点
--------
* 镜像是普通稀疏文件，**逻辑容量** = block_count * 4KiB；文件物理末尾
  另有 spare_count 个 4KiB **备用槽**（默认 2 个），不属于逻辑地址空间。
  宿主实占字节数以 ``stat.st_blocks * 512`` 为准，空洞不占空间。
* 坏块：可对“还在原位置”的块注入读故障（落盘于 ``<镜像>.remap.json``，
  重启仍在）。故障块读请求报错；经原写入入口写入完整新值时触发修复：
  预留一个空闲备用槽 → 新值写入槽内并 fsync → 提交 块->槽 映射并落盘，
  两者都落盘才报写入成功。此后按原块号读写，透明落到备用槽。
* 备用槽只有 2 个：无空槽时修复写失败、坏块状态保留；修复失败或进程
  中断时，未提交的映射不存在，预留槽可再次分配。
* 扫描整段跳过已有空洞：用 ``SEEK_DATA`` / ``SEEK_HOLE`` 一次定位到下一段
  已分配数据，天然跳过空洞（包括之前回收时已打的洞）。
* 只对“已分配且内容全零”的 **整块** 调用
  ``fallocate(FALLOC_FL_PUNCH_HOLE|FALLOC_FL_KEEP_SIZE)`` 打洞。
  打洞保持文件容量不变（KEEP_SIZE），打洞后读回仍是全零，因此
  块读值与盘容量都不变，只释放宿主空间。
* 回收跳过故障块的原位置（不读不打洞）；备用槽里的全零块同样打洞，
  打洞后槽与块号的映射保持不变。
* 每个物理槽（逻辑块 + 备用槽）有一把锁：打洞前在锁内再次校验该块
  仍为全零，与并发写互斥。判零之后若该块被写入非零数据，重检会失败、
  跳过打洞，新数据绝不会被抹掉。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
from typing import Iterator, Optional

from .constants import (
    BLOCK_SIZE,
    DEFAULT_BLOCK_COUNT,
    DEFAULT_SPARE_COUNT,
    FALLOC_FL_KEEP_SIZE,
    FALLOC_FL_PUNCH_HOLE,
    FSYNC_EVERY_PUNCHES,
    SEEK_DATA,
    SEEK_HOLE,
    STATE_DATA,
    STATE_FAULTY,
    STATE_HOLE,
    STATE_ZERO,
)
from .remap import RemapTable

# ---------------------------------------------------------------------------
# libc fallocate(2) 封装（Python 3.11 没有 os.fallocate，直接调 libc）
# ---------------------------------------------------------------------------
_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_libc.fallocate.restype = ctypes.c_int
_libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]


def punch_hole(fd: int, offset: int, length: int) -> None:
    """对 [offset, offset+length) 打洞，保持文件大小不变。

    要求 offset/length 按文件系统块大小对齐，本项目固定按 4KiB 对齐。
    """
    rc = _libc.fallocate(
        fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE, offset, length
    )
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"fallocate(PUNCH_HOLE) failed: {os.strerror(err)}")


def _is_allocated(fd: int, offset: int) -> bool:
    """判断 offset 处是否落在已分配数据区（即不是空洞）。"""
    try:
        data_start = os.lseek(fd, offset, SEEK_DATA)
    except OSError as exc:
        # ENXIO: offset 之后再无数据，全是空洞
        if exc.errno == 6:  # errno.ENXIO
            return False
        raise
    hole_start = os.lseek(fd, offset, SEEK_HOLE)
    return data_start < hole_start


class DiskFaultError(OSError):
    """故障块（未修复）的读请求：模拟介质读错误。"""


class NoSpareSlotError(OSError):
    """无空闲备用槽，修复写入失败（坏块状态保留）。"""


class AlreadyRemappedError(ValueError):
    """块已重映射到备用槽，不在原位置，不能再注入故障。"""


class DiskImage:
    """一个固定容量的 raw 稀疏镜像，物理末尾带备用槽区。"""

    def __init__(
        self,
        path: str,
        block_count: int,
        spare_count: int = DEFAULT_SPARE_COUNT,
        meta_path: Optional[str] = None,
    ):
        self.path = path
        self.block_count = block_count
        self.spare_count = spare_count
        self.capacity = block_count * BLOCK_SIZE  # 虚拟机视角的逻辑容量
        self.physical_blocks = block_count + spare_count  # 末尾 spare_count 个为备用槽
        self.physical_size = self.physical_blocks * BLOCK_SIZE
        self._fd = os.open(path, os.O_RDWR)
        if os.fstat(self._fd).st_size != self.physical_size:
            os.close(self._fd)
            raise ValueError(
                f"镜像 {path} 大小 {os.fstat(self._fd).st_size} 与预期 {self.physical_size} 不一致"
            )
        # 每个物理槽一把锁（逻辑块 0..N-1，备用槽 N..N+S-1），写/修复/打洞互斥
        self._block_locks = [threading.Lock() for _ in range(self.physical_blocks)]
        # 故障与已提交映射（落盘）；备用槽占用（内存态，提交后由元数据重建）
        self._remap = RemapTable(meta_path or (path + ".remap.json"), spare_count, block_count)
        self._alloc_lock = threading.Lock()
        self._slot_owner: list[Optional[int]] = [None] * spare_count  # 已提交：槽 -> 块
        self._reserved: set[int] = set()  # 修复进行中：已预留未提交
        for block, slot in self._remap.snapshot()[1].items():
            self._slot_owner[slot] = block
        # 测试注入点：修复写槽数据落盘后、提交映射前回调（可注入延迟/故障）
        self._slot_write_hook: Optional[callable] = None

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass

    # -- 基本属性 ----------------------------------------------------------
    def actual_bytes(self) -> int:
        """宿主盘上实际占用的字节数（st_blocks 以 512B 计，含备用槽）。"""
        return os.fstat(self._fd).st_blocks * 512

    def stats(self) -> dict:
        return {
            "path": self.path,
            "block_size": BLOCK_SIZE,
            "block_count": self.block_count,
            "spare_count": self.spare_count,
            "capacity_bytes": self.capacity,
            "physical_bytes": self.physical_size,
            "actual_bytes": self.actual_bytes(),
            "hole_bytes": self.physical_size - self.actual_bytes(),
        }

    def _check_index(self, index: int) -> None:
        if not isinstance(index, int) or not (0 <= index < self.block_count):
            raise IndexError(f"块号越界: {index}，合法范围 0..{self.block_count - 1}")

    # -- 坏块故障注入 -------------------------------------------------------
    def inject_fault(self, index: int) -> None:
        """对还在原位置的块注入读故障（幂等）；故障落盘，重启仍在。"""
        self._check_index(index)
        with self._block_locks[index]:  # 与同块修复互斥
            slot = self._remap.slot_of(index)
            if slot is not None:
                raise AlreadyRemappedError(
                    f"块 {index} 已重映射到备用槽 {slot}，不在原位置，不能注入故障"
                )
            self._remap.add_fault(index)  # 幂等；先落盘再生效

    # -- 虚拟机视角的 4KiB 块读写 ------------------------------------------
    def read_block(self, index: int) -> bytes:
        """读一个 4 KiB 块；故障未修复报错，已修复读备用槽，空洞读回全零。"""
        self._check_index(index)
        if self._remap.is_faulty(index):
            slot = self._remap.slot_of(index)
            if slot is None:
                raise DiskFaultError(
                    f"块 {index} 存在读故障且尚未修复（模拟介质读错误）"
                )
            phys = self.block_count + slot
        else:
            phys = index
        with self._block_locks[phys]:
            data = os.pread(self._fd, BLOCK_SIZE, phys * BLOCK_SIZE)
        if len(data) < BLOCK_SIZE:
            data = data + b"\x00" * (BLOCK_SIZE - len(data))
        return data

    def write_block(self, index: int, data: bytes) -> dict:
        """写一个 4KiB 块；故障块触发修复：备用槽接替原位置。

        返回 {"slot": 槽号或 None, "repaired": 本次调用是否完成了修复}。
        """
        self._check_index(index)
        if not isinstance(data, (bytes, bytearray)) or len(data) != BLOCK_SIZE:
            raise ValueError(f"写入数据必须恰好为 {BLOCK_SIZE} 字节")
        if not self._remap.is_faulty(index):
            with self._block_locks[index]:
                os.pwrite(self._fd, bytes(data), index * BLOCK_SIZE)
            return {"slot": None, "repaired": False}
        slot = self._remap.slot_of(index)
        if slot is not None:  # 已修复：写已映射的备用槽
            phys = self.block_count + slot
            with self._block_locks[phys]:
                os.pwrite(self._fd, bytes(data), phys * BLOCK_SIZE)
            return {"slot": slot, "repaired": False}
        return self._repair_and_write(index, bytes(data))

    # -- 坏块修复：备用槽接替原位置 ------------------------------------------
    def _alloc_slot(self) -> int:
        """预留一个空闲备用槽（内存态，提交前崩溃可再次分配）。"""
        with self._alloc_lock:
            for slot in range(self.spare_count):
                if self._slot_owner[slot] is None and slot not in self._reserved:
                    self._reserved.add(slot)
                    return slot
        raise NoSpareSlotError(
            f"数据卷 {self.spare_count} 个备用槽已用完，写入失败（坏块状态保留）"
        )

    def _release_slot(self, slot: int) -> None:
        with self._alloc_lock:
            self._reserved.discard(slot)

    def _write_spare_slot(self, slot: int, data: bytes) -> None:
        """新值写入备用槽并 fsync：内容先落盘，再谈映射提交。"""
        phys = self.block_count + slot
        with self._block_locks[phys]:
            os.pwrite(self._fd, data, phys * BLOCK_SIZE)
            os.fsync(self._fd)
            if self._slot_write_hook is not None:
                self._slot_write_hook(slot)  # 测试注入：延迟 / 故障 / 崩溃

    def _repair_and_write(self, index: int, data: bytes) -> dict:
        """故障未修复块的写入 = 修复：占槽、写内容落盘、提交映射落盘。"""
        with self._block_locks[index]:  # 串行化同块的并发修复
            slot = self._remap.slot_of(index)
            if slot is None:
                slot = self._alloc_slot()  # 无空槽：抛错，坏块状态保留
                try:
                    self._write_spare_slot(slot, data)  # 内容落盘
                    self._remap.commit_mapping(index, slot)  # 映射落盘后才可见
                except Exception:
                    self._release_slot(slot)  # 修复失败：预留槽可再次分配
                    raise
                with self._alloc_lock:
                    self._slot_owner[slot] = index
                    self._reserved.discard(slot)
                return {"slot": slot, "repaired": True}
        # 并发线程已完成修复：本次写入落到已映射的备用槽
        phys = self.block_count + slot
        with self._block_locks[phys]:
            os.pwrite(self._fd, data, phys * BLOCK_SIZE)
        return {"slot": slot, "repaired": False}

    # -- 块状态与快照 ---------------------------------------------------------
    def _physical_state(self, phys: int) -> str:
        """物理槽（逻辑块或备用槽）的 hole / zero / data。"""
        off = phys * BLOCK_SIZE
        with self._block_locks[phys]:
            if not _is_allocated(self._fd, off):
                return STATE_HOLE
            data = os.pread(self._fd, BLOCK_SIZE, off)
            return STATE_ZERO if data == b"\x00" * BLOCK_SIZE else STATE_DATA

    def block_state(self, index: int) -> str:
        """返回 hole / zero / data / faulty；已修复块反映备用槽内容。"""
        self._check_index(index)
        if self._remap.is_faulty(index):
            slot = self._remap.slot_of(index)
            if slot is None:
                return STATE_FAULTY
            return self._physical_state(self.block_count + slot)
        return self._physical_state(index)

    def remap_status(self) -> dict:
        """故障块、重映射去向、备用槽占用与空槽数（供网页展示）。"""
        faulty, mapping = self._remap.snapshot()
        with self._alloc_lock:
            owners = list(self._slot_owner)
            reserved = set(self._reserved)
        slots = []
        for slot in range(self.spare_count):
            owner = owners[slot]
            if owner is not None:
                state = "committed"
            elif slot in reserved:
                state = "reserved"
            else:
                state = "free"
            slots.append({
                "slot": slot,
                "state": state,
                "owner": owner,
                "content": self._physical_state(self.block_count + slot),
            })
        return {
            "spare_count": self.spare_count,
            "spare_free": sum(
                1
                for s in range(self.spare_count)
                if owners[s] is None and s not in reserved
            ),
            "faulty": sorted(faulty),
            "unrepaired": sorted(b for b in faulty if b not in mapping),
            "mapping": {str(b): s for b, s in sorted(mapping.items())},
            "slots": slots,
        }

    def snapshot(self) -> dict:
        """供网页展示的整盘快照（状态 + 容量/实占 + 坏块重映射）。"""
        return {
            "stats": self.stats(),
            "blocks": [self.block_state(i) for i in range(self.block_count)],
            "remap": self.remap_status(),
        }

    # -- 回收扫描 -----------------------------------------------------------
    def reclaim(
        self,
        should_stop: Optional[callable] = None,
        on_event: Optional[callable] = None,
    ) -> Iterator[dict]:
        """生成器：扫描整盘，跳过空洞与故障原位置，给全零整块打洞。

        每处理一个块/槽 yield 一条事件 dict::

            {"index", "state": "punched"|"hole"|"data"|"faulty"|"reserved"|"stopped",
             "actual_bytes"}  # 备用槽事件另带 "slot"

        * ``should_stop()`` 返回 True 时在当前块边界优雅停止（已打的洞保留）。
        * 故障块的原位置不读、不打洞，直接跳过（事件 state="faulty"）。
        * 备用槽里的全零块同样打洞；已提交槽打洞后映射保持不变，
          修复中（已预留）的槽跳过（事件 state="reserved"）。
        * 打洞前在锁内重新校验：必须仍是已分配 + 全零，否则跳过，
          从而保证判零后到达的新写入不会被抹掉。
        * 只打 4KiB 对齐的整块；段首尾未对齐的部分保留不动。
        """
        should_stop = should_stop or (lambda: False)
        on_event = on_event or (lambda ev: None)
        punch_count = 0
        size = self.capacity  # 逻辑区扫描范围（备用槽单独处理）

        # 用 SEEK_DATA 跳到下一段已分配数据，整段跳过已有空洞
        scan = 0
        while scan < size:
            try:
                data_start = os.lseek(self._fd, scan, SEEK_DATA)
            except OSError as exc:
                if exc.errno == 6:  # ENXIO：后面全是空洞
                    break
                raise
            if data_start >= size:
                break
            hole_start = min(os.lseek(self._fd, data_start, SEEK_HOLE), size)

            # 只处理段内按 4KiB 对齐的整块
            first_block = (data_start + BLOCK_SIZE - 1) // BLOCK_SIZE
            last_block = hole_start // BLOCK_SIZE  # 不含
            for index in range(first_block, last_block):
                if should_stop():
                    on_event({"index": index, "state": "stopped", "actual_bytes": self.actual_bytes()})
                    return
                off = index * BLOCK_SIZE
                lock = self._block_locks[index]
                with lock:
                    # 拿到锁后再确认一次：覆盖持锁等待期间收到的中断
                    if should_stop():
                        on_event({"index": index, "state": "stopped", "actual_bytes": self.actual_bytes()})
                        return
                    if self._remap.is_faulty(index):
                        # 故障原位置：不读、不打洞，空间保留
                        event = {"index": index, "state": STATE_FAULTY, "actual_bytes": self.actual_bytes()}
                    elif not _is_allocated(self._fd, off):
                        event = {"index": index, "state": STATE_HOLE, "actual_bytes": self.actual_bytes()}
                    elif os.pread(self._fd, BLOCK_SIZE, off) == b"\x00" * BLOCK_SIZE:
                        # 打洞前二次确认：已分配且全零（与 write_block 互斥）
                        punch_hole(self._fd, off, BLOCK_SIZE)
                        punch_count += 1
                        if punch_count % FSYNC_EVERY_PUNCHES == 0:
                            os.fsync(self._fd)
                        event = {"index": index, "state": "punched", "actual_bytes": self.actual_bytes()}
                    else:
                        event = {"index": index, "state": STATE_DATA, "actual_bytes": self.actual_bytes()}
                on_event(event)
                yield event

            # 段内未对齐的零碎部分不打洞，直接进入下一段扫描
            scan = hole_start

        # 备用槽区：全零槽打洞（已提交槽映射保留），修复中的槽跳过
        for slot in range(self.spare_count):
            phys = self.block_count + slot
            if should_stop():
                on_event({"index": phys, "slot": slot, "state": "stopped", "actual_bytes": self.actual_bytes()})
                return
            with self._block_locks[phys]:
                if should_stop():
                    on_event({"index": phys, "slot": slot, "state": "stopped", "actual_bytes": self.actual_bytes()})
                    return
                if self._is_reserved(slot):
                    event = {"index": phys, "slot": slot, "state": "reserved", "actual_bytes": self.actual_bytes()}
                elif not _is_allocated(self._fd, phys * BLOCK_SIZE):
                    event = {"index": phys, "slot": slot, "state": STATE_HOLE, "actual_bytes": self.actual_bytes()}
                elif os.pread(self._fd, BLOCK_SIZE, phys * BLOCK_SIZE) == b"\x00" * BLOCK_SIZE:
                    punch_hole(self._fd, phys * BLOCK_SIZE, BLOCK_SIZE)
                    punch_count += 1
                    if punch_count % FSYNC_EVERY_PUNCHES == 0:
                        os.fsync(self._fd)
                    event = {"index": phys, "slot": slot, "state": "punched", "actual_bytes": self.actual_bytes()}
                else:
                    event = {"index": phys, "slot": slot, "state": STATE_DATA, "actual_bytes": self.actual_bytes()}
            on_event(event)
            yield event

        os.fsync(self._fd)

    def _is_reserved(self, slot: int) -> bool:
        with self._alloc_lock:
            return slot in self._reserved


# ---------------------------------------------------------------------------
# 预置镜像
# ---------------------------------------------------------------------------
def create_image(
    path: str,
    block_count: int = DEFAULT_BLOCK_COUNT,
    spare_count: int = DEFAULT_SPARE_COUNT,
) -> DiskImage:
    """创建（或打开已有的）预置 raw 镜像。

    预置布局（256 块 + 2 备用槽时）::

        块 0..63    空洞（ftruncate 产生，不占宿主空间）
        块 64..127  写过的全零块（已分配、占空间、可回收）
        块 128..191 非零块（含块号标记，不可回收）
        块 192..255 空洞
        槽 0..1     备用槽（空洞，坏块修复时接替原位置）

    块数不足时按 1/4 比例划分，保证三类块都存在。
    镜像已存在且容量正确时直接复用，保证重启后原盘、已打的洞、
    故障标记与已提交映射都保留；旧版无备用槽布局的镜像会在尾部
    追加备用区（空洞）后复用。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    meta_path = path + ".remap.json"
    physical = (block_count + spare_count) * BLOCK_SIZE
    if os.path.exists(path):
        size = os.path.getsize(path)
        if size == physical:
            return DiskImage(path, block_count, spare_count)
        if size == block_count * BLOCK_SIZE:
            # 旧版无备用槽布局：尾部追加备用区（空洞），映射元数据从头开始
            fd = os.open(path, os.O_RDWR)
            try:
                os.ftruncate(fd, physical)
                os.fsync(fd)
            finally:
                os.close(fd)
            return DiskImage(path, block_count, spare_count)
        # 容量不符：落到重建分支，旧映射一并作废

    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, physical)  # 逻辑区与备用槽先全是空洞
        quarter = max(1, block_count // 4)
        zero_start, zero_end = quarter, quarter * 2
        data_start, data_end = quarter * 2, quarter * 3

        # 写过的零块：显式写 4KiB 零，文件系统会分配宿主块
        for i in range(zero_start, zero_end):
            os.pwrite(fd, b"\x00" * BLOCK_SIZE, i * BLOCK_SIZE)
        # 非零块：写入带块号的可辨识图案
        for i in range(data_start, min(data_end, block_count)):
            marker = f"block-{i:04d}\n".encode()
            payload = (marker + b"\xaa" * BLOCK_SIZE)[:BLOCK_SIZE]
            os.pwrite(fd, payload, i * BLOCK_SIZE)
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.path.exists(meta_path):
        os.remove(meta_path)  # 镜像重建，旧故障/映射元数据随之作废
    return DiskImage(path, block_count, spare_count)
