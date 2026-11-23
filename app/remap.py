"""坏块故障与重映射元数据的持久化（RemapTable）。

元数据与镜像分开存放在同一数据卷：``<镜像路径>.remap.json``，内容::

    {"version": 1, "faulty": [5, 9], "mapping": {"5": 0}}

* ``faulty``：已注入读故障的逻辑块号。故障块的原位置永久不可读，
  重启后故障仍在。
* ``mapping``：**已提交**的 逻辑块号 -> 备用槽号 映射。只有内容落盘
  （镜像 fsync）之后才会提交映射；映射提交前崩溃，备用槽不算被占用。

持久化采用“写临时文件 + fsync + rename + 目录 fsync”的原子替换，
任何时候读者看到的都是完整的一份元数据。内存中的映射在**落盘成功后**
才更新，因此“内容和映射都落盘才报写入成功”。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, Optional, Set, Tuple

META_VERSION = 1


class RemapTable:
    """故障块集合 + 已提交映射，带原子落盘。"""

    def __init__(self, meta_path: str, spare_count: int, block_count: int):
        self.meta_path = meta_path
        self.spare_count = spare_count
        self.block_count = block_count
        self._lock = threading.Lock()  # 序列化更新与落盘
        self._faulty: Set[int] = set()
        self._mapping: Dict[int, int] = {}
        self._load()

    # -- 加载与校验 ---------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.meta_path):
            return
        with open(self.meta_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if raw.get("version") != META_VERSION:
            raise ValueError(f"映射元数据版本不支持: {raw.get('version')!r}")
        faulty = raw.get("faulty", [])
        mapping = raw.get("mapping", {})
        if not isinstance(faulty, list) or not isinstance(mapping, dict):
            raise ValueError("映射元数据格式非法")
        for b in faulty:
            self._check_block(int(b))
        for b_str, s in mapping.items():
            b, s = int(b_str), int(s)
            self._check_block(b)
            if not (0 <= s < self.spare_count):
                raise ValueError(f"映射元数据中的备用槽号越界: {s}")
            self._mapping[b] = s
        self._faulty = {int(b) for b in faulty}
        # 已映射的块必然也是故障块，补齐以容忍旧版/手工编辑的元数据
        self._faulty |= set(self._mapping)

    def _check_block(self, block: int) -> None:
        if not (0 <= block < self.block_count):
            raise ValueError(f"映射元数据中的块号越界: {block}")

    # -- 原子落盘 -------------------------------------------------------------
    def _persist_locked(self) -> None:
        """调用方必须持有 self._lock。原子替换 + 目录 fsync。"""
        payload = {
            "version": META_VERSION,
            "faulty": sorted(self._faulty),
            "mapping": {str(b): s for b, s in sorted(self._mapping.items())},
        }
        tmp_path = self.meta_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.meta_path)
        dir_fd = os.open(
            os.path.dirname(os.path.abspath(self.meta_path)), os.O_RDONLY
        )
        try:
            os.fsync(dir_fd)  # 保证 rename 本身落盘
        finally:
            os.close(dir_fd)

    # -- 更新（先落盘，再生效） -------------------------------------------------
    def add_fault(self, block: int) -> None:
        """登记故障块并落盘；已存在时为幂等空操作。"""
        with self._lock:
            if block in self._faulty:
                return
            self._faulty.add(block)
            try:
                self._persist_locked()
            except Exception:
                self._faulty.discard(block)  # 落盘失败：内存态一并回滚
                raise

    def commit_mapping(self, block: int, slot: int) -> None:
        """提交 块->槽 映射：先落盘，落盘成功后映射才对读者可见。"""
        with self._lock:
            self._mapping[block] = slot
            try:
                self._persist_locked()
            except Exception:
                del self._mapping[block]  # 落盘失败：映射不生效，块仍报读错
                raise

    # -- 只读视图（dict/set 读取在 GIL 下原子，读者无需拿锁） -------------------
    def is_faulty(self, block: int) -> bool:
        return block in self._faulty

    def slot_of(self, block: int) -> Optional[int]:
        return self._mapping.get(block)

    def snapshot(self) -> Tuple[Set[int], Dict[int, int]]:
        return set(self._faulty), dict(self._mapping)
