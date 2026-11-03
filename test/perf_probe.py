"""性能探针（只输出数字，不做断言）。

对应 BIG_SCALE_REPORT 的两项指标：
  - build_block：每轮对话都要跑一次
  - detail 检索：扫描 DL3 + DL2，再顺着 sources 下探

用法：python astrbot/test/perf_probe.py [每层条数，默认 100000]
不读写真实记忆库，全部在临时目录。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "core")
sys.path.insert(0, CORE)

from tiers import Tiers  # noqa: E402

NOW = 1762000000000
DAY = 86400000


def write_layer(root: str, name: str, items: list[dict]) -> None:
    with open(os.path.join(root, f"{name}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": 2, "at": 0, "items": items}, f, ensure_ascii=False)


def build(n: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"L1": [], "L2": [], "DL1": [], "DL2": [], "DL3": [], "L3": []}
    for i in range(n):
        out["L2"].append({
            "id": f"b{i}", "text": f"缓冲条目 {i} 报告 markdown", "layer": "L2",
            "speaker": "user", "people": [], "scene": "qq·项目群",
            "importance": 0.5, "heat": 1, "repeatStrength": 1,
            "createdAt": NOW - (i % 30) * DAY, "consolidatedAt": NOW - (i % 30) * DAY,
            "origin": "astrbot", "kind": "raw",
        })
        out["DL1"].append({
            "id": f"d{i}", "text": f"流水条目 {i} 报告", "layer": "DL1",
            "speaker": "user", "people": [], "scene": "",
            "importance": 0.5, "heat": 1, "repeatStrength": 1,
            "createdAt": NOW - (i % 30) * DAY, "origin": "astrbot", "kind": "raw",
        })
        out["DL3"].append({
            "id": f"s{i}", "text": f"摘要 {i} 报告 markdown", "layer": "DL3",
            "kind": "summary", "speaker": "", "people": [], "scene": "",
            "importance": 0.7, "heat": 2, "repeatStrength": 1,
            "createdAt": NOW - (i % 30) * DAY,
            "spanFrom": NOW - 30 * DAY, "spanTo": NOW - (i % 30) * DAY,
            "sources": [f"d{i}"], "origin": "astrbot",
        })
    return out


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100000
    root = tempfile.mkdtemp(prefix="perf-")
    try:
        t0 = time.perf_counter()
        layers = build(n)
        for name, items in layers.items():
            write_layer(root, name, items)
        print(f"fixture: 每层 {n} 条，写完 {time.perf_counter() - t0:.1f}s")

        tiers = Tiers(root, writer="astrbot", hot_max_chars=3000,
                      long_max_chars=4000, consolidate_max_chars=2500)

        t0 = time.perf_counter()
        b1 = tiers.build_block()
        t1 = time.perf_counter()
        b2 = tiers.build_block()
        t2 = time.perf_counter()
        assert b1 == b2
        print(f"build_block  首次 {t1 - t0:.3f}s · 缓存命中 {t2 - t1:.3f}s · 注入 {len(b1)} 字")

        r1 = await tiers.search("报告 markdown", depth="detail", limit=6, vectors={})
        r1_t = time.perf_counter() - t1
        t3 = time.perf_counter()
        r2 = await tiers.search("报告 markdown", depth="detail", limit=6, vectors={})
        r2_t = time.perf_counter() - t3
        ids1 = [h["id"] for h in r1["hits"]]
        ids2 = [h["id"] for h in r2["hits"]]
        assert ids1 == ids2, "两次检索结果不一致"
        print(f"detail 检索  首次 {r1_t:.3f}s · 缓存命中 {r2_t:.3f}s · "
              f"命中 {len(ids1)} 条（含 sources 下探）")
        print(f"缓存 hit/miss = {tiers.cache_hits}/{tiers.cache_misses}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
