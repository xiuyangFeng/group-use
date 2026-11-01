"""journal.jsonl：追加写的操作日志，用于排障和「查看修复日志」。

注意：数据安全不依赖 journal——恢复逻辑以 manifest + 文件系统实际状态为准，
journal 丢了最多丢失诊断信息，不会丢数据。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.seq = 0
        self._fh = None

    def open(self) -> "Journal":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        return self

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def append(self, op: str, **fields) -> None:
        if self._fh is None:
            return
        self.seq += 1
        rec = {"seq": self.seq, "ts": round(time.time(), 3), "op": op}
        rec.update(fields)
        self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        # 每 16 条 fsync 一次，避免拖慢大批量文件处理
        if self.seq % 16 == 0:
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def __enter__(self) -> "Journal":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()
