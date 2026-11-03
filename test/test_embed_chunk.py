"""A2 回归：嵌入按 32 条分批、有界并发，单批失败不连累整组。

原先整组一次传给 provider：10 万条必然被拒，且失败粒度是"整组"。
现在与 dsh 的 embedInto(chunk=32) 对齐。

用法：python astrbot/test/test_embed_chunk.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

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


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


async def main() -> None:
    root = tempfile.mkdtemp(prefix="a2-embed-")
    try:
        org = Organizer(root, "astrbot", {"embed_chunk": 32, "embed_concurrency": 4})
        items = [{"id": f"e{i}", "text": f"文本{i}"} for i in range(100)]

        # —— 全部成功：4 批，每批 ≤32 ——
        store = FakeStore()
        org.vectors = store
        sizes: list[int] = []

        async def ok(texts):
            sizes.append(len(texts))
            return [[1.0, 2.0] for _ in texts]

        n = await org._embed_items(items, ok)
        assert_(len(sizes) == 4, f"100 条应分 4 批，实际 {sizes}")
        assert_(all(s <= 32 for s in sizes), f"每批不得超过 32 条，实际 {sizes}")
        assert_(n == 100 and len(store.mem) == 100, f"应写入 100 条，实际 {n}")

        # —— 一批失败：只丢那一批（32 条），其余 68 条照常写入 ——
        store2 = FakeStore()
        org.vectors = store2

        async def flaky(texts):
            if "文本32" in texts[0]:
                raise RuntimeError("boom")
            return [[1.0, 2.0] for _ in texts]

        n2 = await org._embed_items(items, flaky)
        assert_(n2 == 68, f"失败批只应丢 32 条，实际写入 {n2} 条")
        assert_(len(store2.mem) == 68, f"向量数应为 68，实际 {len(store2.mem)}")

        print(f"PASS A2：100 条 → 4 批 {sizes}（每批 ≤32）；一批失败只丢 {100 - n2} 条")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
