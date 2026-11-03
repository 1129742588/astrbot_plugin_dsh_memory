"""A4 回归：遗忘 DL1 时把向量一起删掉。

原来 forget_dl1 只写台账、不删向量，跑久了向量库里堆满已遗忘条目：
占空间、还会被 fuse 的预筛扫到。dsh 侧一直有 vectors.delete。

用法：python astrbot/test/test_forget_vectors.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "core")
sys.path.insert(0, CORE)

from organizer import Organizer  # noqa: E402


class FakeStore:
    def __init__(self) -> None:
        self.mem: dict[str, list[float]] = {}

    def writable(self):
        return True, ""

    def get(self, i):
        return self.mem.get(i)

    def remember(self, i, v):
        self.mem[i] = v

    def forget(self, i):
        self.mem.pop(i, None)


def main() -> None:
    root = tempfile.mkdtemp(prefix="a4-vec-")
    try:
        org = Organizer(root, "astrbot", {"forget_threshold": 0.25})
        store = FakeStore()
        store.mem["old"] = [1.0, 0.0]
        store.mem["keep"] = [0.0, 1.0]
        org.vectors = store

        old_ts = int(time.time() * 1000) - 100 * 86400000
        db = {
            "DL1": [
                {"id": "old", "text": "很旧的流水账", "createdAt": old_ts,
                 "lastMentionedAt": old_ts, "heat": 1, "repeatStrength": 1},
                {"id": "keep", "text": "刚写的一条", "createdAt": int(time.time() * 1000),
                 "lastMentionedAt": int(time.time() * 1000), "heat": 1, "repeatStrength": 1},
            ]
        }

        n = org.forget_dl1(db)
        assert n == 1, f"应忘掉 1 条，实际 {n}"
        assert "old" not in store.mem, "遗忘后向量应该一起删掉（否则是孤儿向量）"
        assert "keep" in store.mem, "没被忘的条目向量不能被误删"

        print("PASS A4：DL1 遗忘时向量同步删除，未遗忘的条目不受影响")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
