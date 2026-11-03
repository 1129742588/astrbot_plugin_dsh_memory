"""只读对照脚本（astrbot 侧）。

用同一份固定 fixture 跑「协议层 + 整理层」的纯函数，把结果导出成 JSON，
供 compare_parity.py 与 dsh 侧的输出逐字段比对。

不修改 core，不读写真实记忆库：fixture 全部落在系统临时目录，跑完即删。

用法：python astrbot/test/parity_dump.py
输出：astrbot/test/out/astrbot.json
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "core")
sys.path.insert(0, CORE)

from tiers import Tiers  # noqa: E402
from organizer import pick_turn_batch  # noqa: E402

BASE = 1762000000000
OUT_DIR = os.path.join(HERE, "out")
LONG_MAX_CHARS = 1500
HOT_MAX_CHARS = 3000


def item(**kw):
    """字段顺序无关，缺省的按 make_item 的默认值补齐。"""
    base = {
        "id": "",
        "seq": 0,
        "text": "",
        "kind": "raw",
        "channel": "dialog",
        "layer": "L1",
        "speaker": "",
        "people": [],
        "scene": "",
        "importance": 0.5,
        "directEntry": False,
        "heat": 1,
        "lastMentionedAt": BASE,
        "repeatStrength": 1,
        "fusionCount": 0,
        "recallCount": 0,
        "supersededBy": None,
        "createdAt": BASE,
        "archivedAt": None,
        "spanFrom": None,
        "spanTo": None,
        "pinned": False,
        "archiveFile": None,
        "origin": "astrbot",
    }
    base.update(kw)
    return base


def fixture_layers():
    """主 fixture：3 条 L1、2 条 L2、1 条 DL1、1 条 DL2、4 条 DL3（1 条已作废）。"""
    l1 = [
        item(id="m5_p1", seq=1, text="今天把召回率调到 0.88", speaker="user",
             people=["小鱼"], scene="qq·项目群", createdAt=BASE, turnId="t1"),
        item(id="m5_p2", seq=2, text="记下了，回头提醒你", speaker="bot",
             people=[], scene="qq·项目群", createdAt=BASE + 1000, turnId="t1"),
        item(id="m5_p3", seq=3, text="明天要交报告", speaker="user", people=[],
             scene="qq·项目群", createdAt=BASE + 2000, turnId="t2", origin="dsh"),
    ]
    l2 = [
        item(id="m5_b1", seq=4, text="约定：报告统一用 markdown", layer="L2",
             speaker="user", createdAt=BASE - 200000000,
             consolidatedAt=BASE - 100000000, origin="dsh"),
        item(id="m5_b2", seq=5, text="旧的一条缓冲记录", layer="L2",
             createdAt=BASE - 300000000, consolidatedAt=BASE - 200000000),
    ]
    dl1 = [
        item(id="m5_d1", seq=6, text="流水账一条：中午吃了面", layer="DL1",
             createdAt=BASE - 500000000),
    ]
    dl2 = [
        item(id="m5_d2", seq=7, text="被摘要引用的原文", layer="DL2",
             createdAt=BASE - 600000000, consolidatedAt=BASE - 600000000, origin="dsh"),
    ]
    dl3 = [
        item(id="m5_s0", seq=8, text="长期摘要：报告统一 markdown", kind="summary",
             layer="DL3", speaker="", createdAt=BASE - 600000000,
             spanFrom=BASE - 600000000, spanTo=BASE - 600000000,
             importance=0.9, heat=2, sources=["m5_d2"], origin="dsh", srcKey="k0"),
        item(id="m5_s1", seq=9, text="长摘要B" + "乙" * 736, kind="summary",
             layer="DL3", createdAt=BASE - 500000000,
             spanFrom=BASE - 500000000, spanTo=BASE - 500000000,
             importance=0.7, heat=2, sources=[], origin="dsh", srcKey="k1"),
        item(id="m5_s2", seq=10, text="长摘要C" + "丙" * 736, kind="summary",
             layer="DL3", createdAt=BASE - 400000000,
             spanFrom=BASE - 400000000, spanTo=BASE - 400000000,
             importance=0.7, heat=2, sources=[], origin="dsh", srcKey="k2"),
        item(id="m5_s3", seq=11, text="已作废摘要", kind="summary", layer="DL3",
             createdAt=BASE - 300000000, importance=0.95, heat=2,
             supersededBy="m5_s0", sources=[], origin="dsh", srcKey="k3"),
    ]
    return {"L1": l1, "L2": l2, "DL1": dl1, "DL2": dl2, "DL3": dl3}


def fixture_temps():
    """三个 temp：自己的、另一端的、以及一个已认领未落盘的（.claimed-）。"""
    return {
        "parity.jsonl": [
            item(id="m5_p4", seq=12, text="临时的一条", speaker="user", people=[],
                 createdAt=BASE + 3000, turnId="t3"),
        ],
        "other.jsonl": [
            item(id="m5_p5", seq=13, text="另一端的临时", speaker="bot", people=[],
                 createdAt=BASE + 3500, turnId="t4", origin="dsh"),
        ],
        "x.claimed-1762000000000-abcd.jsonl": [
            item(id="m5_p6", seq=14, text="已认领未落盘", speaker="user", people=[],
                 createdAt=BASE + 4000, turnId="t5", origin="dsh"),
        ],
    }


def write_layer(root: str, name: str, items: list[dict]) -> None:
    payload = {"version": 2, "at": BASE, "items": items}
    with open(os.path.join(root, f"{name}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False)


def build_fixture(root: str) -> None:
    os.makedirs(os.path.join(root, "L1.tmp"), exist_ok=True)
    layers = fixture_layers()
    for name, items in layers.items():
        write_layer(root, name, items)
    for fname, rows in fixture_temps().items():
        with open(os.path.join(root, "L1.tmp", fname), "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def pick_fixture():
    """6 个独立轮次、每条 1 字，cap=6 / max_items=5：考的是 items 目标的取整差异。"""
    return [
        item(id=f"k{i}", seq=100 + i, text="一", turnId=f"u{i}",
             createdAt=BASE + i * 1000)
        for i in range(1, 7)
    ]


async def main() -> None:
    root = tempfile.mkdtemp(prefix="parity-astrbot-")
    try:
        build_fixture(root)
        tiers = Tiers(
            dir_path=root,
            writer="astrbot",
            hot_max_chars=HOT_MAX_CHARS,
            hot_quota_own=0.7,
            long_max_chars=LONG_MAX_CHARS,
            consolidate_max_chars=2500,
        )

        block = tiers.build_block()
        l1_view, _bad = tiers.read_l1_view()

        pick = pick_turn_batch(
            pick_fixture(), 6,
            order_key=lambda it: it.get("createdAt") or 0,
            max_items=5,
        )

        made = tiers.make_item(text="测试", speaker="user", people=[])
        cosine_uneq = Tiers._cosine([1.0, 2.0], [1.0, 2.0, 3.0])

        searched = await tiers.search("报告 markdown", depth="detail", limit=6, vectors={})

        out = {
            "side": "astrbot",
            "block": block,
            "l1_view_ids": [it["id"] for it in l1_view],
            "l1_view_count": len(l1_view),
            "pick_ids": [it["id"] for it in pick],
            "cosine_unequal": round(cosine_uneq, 6),
            "make_item_people": made["people"],
            "search_depth": searched["depth"],
            "search_hits": [
                {"id": h["id"], "score": h["score"], "layer": h["layer"]}
                for h in searched["hits"]
            ],
            "search_confidence": searched["confidence"],
        }
        os.makedirs(OUT_DIR, exist_ok=True)
        dst = os.path.join(OUT_DIR, "astrbot.json")
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"OK -> {dst}")
        print(f"  l1_view={len(l1_view)} pick={[it['id'] for it in pick]} "
              f"cos={cosine_uneq:.4f} people={made['people']}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
