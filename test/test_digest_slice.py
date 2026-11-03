"""A1 回归：摘要按 summary_chunk 切片，片内编号合并时加回偏移。

与 dsh 的 silicon.summarizeWithSources 分片对齐：
  - 每片 ≤ summary_chunk 条，编号从 1 重新开始
  - 合并时 sources / keep 加回片起点偏移
  - 单片失败只损失那一片（其原文走 DL1，不丢）

用法：python astrbot/test/test_digest_slice.py
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


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


async def main() -> None:
    root = tempfile.mkdtemp(prefix="a1-slice-")
    try:
        org = Organizer(root, "astrbot", {"summary_chunk": 3})
        group = [
            {"id": f"g{i}", "text": f"原文{i}", "speaker": "user"}
            for i in range(1, 11)
        ]

        calls: list[int] = []

        async def summarize(lines, channel, folks, hint):
            calls.append(len(lines))
            return f"★ 摘要 {lines[0]}《1》\n"

        res = await org._summarize_group(group, summarize, "dialog", ["小鱼"], "")
        assert_(len(calls) == 4, f"10 条 / 片长 3 应调用 4 次，实际 {calls}")
        assert_(all(n <= 3 for n in calls), f"每片不得超过 3 条，实际 {calls}")
        srcs = [s["sources"] for s in res["summaries"]]
        assert_(srcs == [[1], [4], [7], [10]], f"编号偏移不对：{srcs}")

        # 单片失败：第 2 片抛异常，其余片照常，且偏移仍然正确
        calls.clear()

        async def flaky(lines, channel, folks, hint):
            calls.append(len(lines))
            if len(calls) == 2:
                raise RuntimeError("boom")
            return f"★ 摘要 {lines[0]}《1》\n"

        res2 = await org._summarize_group(group, flaky, "dialog", [], "")
        srcs2 = [s["sources"] for s in res2["summaries"]]
        assert_(srcs2 == [[1], [7], [10]], f"单片失败应只损失该片，实际 {srcs2}")

        print("PASS A1：按 3 条切片，编号偏移 1/4/7/10；单片失败只损失该片")
        print(f"  每片条数 {calls}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
