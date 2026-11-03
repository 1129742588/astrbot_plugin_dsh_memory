"""批次 3 回归：打分优化后检索结果逐条不变。

优化做了三件事（不改变公式与排序）：
  1. 关键词命中改逐字段短路：先查 text，没命中的关键词才去拼 speaker/people/scene
  2. 只有通过过滤的条目才建 dict
  3. 排序改 top-k（heapq.nsmallest），同分按原序 —— 与稳定排序取前 k 完全一致

这里用一份朴素参考实现（拼 body + 全量排序）跑同样的数据与查询，逐条比对
(id, score)。覆盖：text 命中、只命中 people、只命中 scene、时间窗、已作废放行、
同分（验证 top-k 的稳定性）。

用法：python astrbot/test/test_search_equiv.py
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "core")
sys.path.insert(0, CORE)

from tiers import (  # noqa: E402
    DEPTH_LAYERS,
    Tiers,
    DETAIL_HINTS,
    keywords,
    layer_boost,
    parse_time,
)

NOW = 1762000000000
DAY = 86400000


def write_layer(root: str, name: str, items: list[dict]) -> None:
    with open(os.path.join(root, f"{name}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": 2, "at": 0, "items": items}, f, ensure_ascii=False)


def make_fixture() -> dict[str, list[dict]]:
    rnd = random.Random(20261102)
    words = ["报告", "markdown", "召回率", "肺结节", "竞赛", "缓存", "检索", "摘要", "向量", "记忆"]
    layers = {name: [] for name in DEPTH_LAYERS["full"]}

    def mk(i: int, layer: str, **kw) -> dict:
        base = {
            "id": f"{layer}-{i}",
            "text": f"{rnd.choice(words)} 第{i}条",
            "layer": layer,
            "speaker": "user",
            "people": [],
            "scene": "",
            "importance": rnd.choice([0.5, 0.7, 0.9]),
            "createdAt": NOW - rnd.randrange(1, 40) * DAY,
            "heat": 1,
            "repeatStrength": 1,
            "origin": "astrbot",
            "kind": "raw",
        }
        base.update(kw)
        return base

    for i in range(120):
        layers["DL1"].append(mk(i, "DL1"))
    for i in range(60):
        layers["DL2"].append(mk(i, "DL2"))
    for i in range(40):
        layers["DL3"].append(mk(i, "DL3", kind="summary",
                                spanFrom=NOW - 30 * DAY, spanTo=NOW - 20 * DAY))

    # people / scene 命中（body 能命中、text 命中不了）
    layers["DL1"].append(mk(900, "DL1", text="普通一条", people=["小鱼"]))
    layers["DL1"].append(mk(901, "DL1", text="普通一条", scene="qq·项目群"))
    # 已作废（只有查询里带"以前/之前"才放行）
    layers["DL1"].append(mk(902, "DL1", text="报告 旧的约定", supersededBy="DL3-0"))
    # 同分：同等 importance、同样长度、同一个时间 → 排序要靠原序稳定
    for j in range(8):
        layers["DL2"].append(mk(800 + j, "DL2", text="同分条目", importance=0.7,
                                createdAt=NOW - 5 * DAY, people=[], scene=""))
    return layers


def naive_search(tiers: Tiers, q: str, depth: str, limit: int) -> list[tuple]:
    """改动前的实现：拼 body + 全量排序。"""
    if depth not in DEPTH_LAYERS:
        depth = "detail" if DETAIL_HINTS.search(q) else "summary"
    win = parse_time(q, NOW)
    kw = keywords(q.replace(win["word"], "") if win else q)
    pool: list[dict] = []
    for l in DEPTH_LAYERS[depth]:
        pool.extend(tiers.read_layer_safe(l))

    scored = []
    for it in pool:
        if it.get("supersededBy") and not __import__("re").search("以前|之前", q):
            continue
        frm = int(it.get("spanFrom") or it.get("createdAt") or 0)
        to = int(it.get("spanTo") or it.get("createdAt") or 0)
        if win and not (to >= win["from"] and frm <= win["to"]):
            continue
        body = " ".join([
            str(it.get("text") or ""),
            str(it.get("speaker") or ""),
            " ".join(str(x) for x in (it.get("people") or [])),
            str(it.get("scene") or ""),
        ])
        hit = sum(1 for k in kw if k in body)
        coverage = (hit / len(kw)) if kw else 0.0
        if kw and hit == 0 and not win:
            continue
        time_score = 0.9 if win else 0.5
        imp = float(it.get("importance") or 0)
        boost = layer_boost(str(it.get("layer") or ""))
        score = 0.55 * coverage + 0.25 * time_score + 0.12 * imp + 0.08 * boost
        scored.append({"it": it, "score": score})
    scored.sort(key=lambda x: -x["score"])
    return [(x["it"]["id"], round(x["score"], 4)) for x in scored[:limit]]


async def main() -> None:
    root = tempfile.mkdtemp(prefix="search-equiv-")
    try:
        layers = make_fixture()
        for name, items in layers.items():
            write_layer(root, name, items)
        write_layer(root, "L1", [])
        write_layer(root, "L3", [])

        tiers = Tiers(root, writer="astrbot")

        queries = [
            ("报告 markdown", "detail"),
            ("小鱼", "detail"),
            ("项目群", "detail"),
            ("昨天", "full"),
            ("以前的约定", "detail"),
            ("完全无关的词", "detail"),
            ("召回率", "summary"),
            ("交报告", "full"),
            ("markdown", "detail"),
            ("群", "detail"),
        ]

        for q, depth in queries:
            want = naive_search(tiers, q, depth, 6)
            got_res = await tiers.search(
                q, depth=depth, limit=6, vectors={}, follow_sources=False, now=NOW,
            )
            got = [(h["id"], h["score"]) for h in got_res["hits"]]
            assert want == got, (
                f"查询「{q}」({depth}) 不一致：\n  参考 {want}\n  现在 {got}"
            )

        # 同分场景单独确认：8 条同分，取 6 条时必须按原序（DL2-800..805）
        same = await tiers.search("同分条目", depth="detail", limit=6, vectors={},
                                  follow_sources=False, now=NOW)
        ids = [h["id"] for h in same["hits"]]
        assert ids == [f"DL2-{800 + j}" for j in range(6)], f"同分排序不稳定：{ids}"

        # —— sem 字段：语义注入要靠它卡门槛（不是混合分 score）——
        one = await tiers.search("报告", depth="detail", limit=6, vectors={},
                                 follow_sources=False, now=NOW)
        assert all("sem" in h and h["sem"] >= 0 for h in one["hits"]), "hits 缺 sem 字段"

        # —— rerank：兼容 [{index, score}]，并按 rel 重排、给 advice 换阈值 ——
        async def fake_rerank(q, docs):
            return [{"index": i, "score": 0.9 - i * 0.1} for i in range(len(docs))]

        rr = await tiers.search("报告", depth="detail", limit=6, vectors={},
                                follow_sources=False, now=NOW, rerank=fake_rerank)
        assert rr["reranked"] is True, "有 rerank 时 reranked 应为 True"
        assert all(h["rel"] is not None for h in rr["hits"]), "精排后 rel 不能为空"
        rels = [h["rel"] for h in rr["hits"]]
        assert rels == sorted(rels, reverse=True), f"精排结果未按 rel 降序：{rels}"
        assert rr["advice"].startswith("相关度较高"), f"精排后 advice 阈值不对：{rr['advice']}"

        print(f"PASS 批次 3：{len(queries)} 个查询的 (id, score) 与朴素实现逐条一致；同分按原序")
        print("PASS 批次 4：hits 带 sem；rerank 兼容 [{index,score}]，按 rel 重排且 advice 用精排阈值")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
