"""A3 回归：没有模型时不产生假摘要，批次下沉 DL1 并免于遗忘。

约定（2026-11-02 定）：
  - 没有摘要能力 → L1 溢出的批次进 DL1（深层层，不进上下文），打 pendingDigest
  - L2 不动（它是会进上下文的缓冲层，别在没法压的时候清空）
  - pendingDigest 的条目不参与遗忘（否则"等模型"等成丢失）
  - 有模型后，L2 满时组织 L2 的批次会带上 DL1 全部 → 那时才真正压摘要

用法：python astrbot/test/test_no_model_dl1.py
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
from organizer import Organizer  # noqa: E402

DAY = 86400000
OLD = int(time.time() * 1000) - 100 * DAY


def write_layer(root: str, name: str, items: list[dict]) -> None:
    with open(os.path.join(root, f"{name}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": 2, "at": 0, "items": items}, f, ensure_ascii=False)


def read_layer(root: str, name: str) -> list[dict]:
    p = os.path.join(root, f"{name}.json")
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f).get("items") or []


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


async def main() -> None:
    root = tempfile.mkdtemp(prefix="a3-nomodel-")
    try:
        tiers = Tiers(
            dir_path=root, writer="astrbot",
            hot_max_chars=5,
            long_max_chars=1500, consolidate_max_chars=2500,
        )
        # 两条 100 天前的原文：字数远超 hot_max_chars，且保持率远低于遗忘阈值
        rows = []
        for i in range(2):
            it = tiers.make_item(text=f"第一百天前的原文{i}", speaker="user")
            it["createdAt"] = OLD
            it["lastMentionedAt"] = OLD
            it["seq"] = i + 1
            rows.append(it)
        write_layer(root, "L1", rows)
        for name in ("L2", "DL1", "DL2", "DL3"):
            write_layer(root, name, [])

        org = Organizer(root, "astrbot", {
            "hot_max_chars": 5,
            "hot_max_items": 10,
            "consolidate_max_chars": 2500,
            "long_max_chars": 1500,
            "quiet_ms": 0,
            "forget_threshold": 0.25,
            "l2_keep_max": 10,
        })

        # —— 第一次整理：没有模型 ——
        res = await org.organize(None, None, None)
        assert_(res.get("ok"), f"整理应成功（不再因无模型整轮跳过），实际 {res}")

        l1 = read_layer(root, "L1")
        l2 = read_layer(root, "L2")
        dl1 = read_layer(root, "DL1")
        assert_(len(l1) == 0, f"L1 应被清空（不挤上下文），实际还剩 {len(l1)} 条")
        assert_(len(l2) == 0, f"L2 不应被动过，实际有 {len(l2)} 条")
        assert_(len(dl1) == 2, f"两条原文应进 DL1，实际 {len(dl1)} 条")
        assert_(all(it.get("pendingDigest") for it in dl1),
                "进 DL1 的条目应带 pendingDigest 标记")

        # —— 第二次整理：触发 forgetDL1 ——
        # 这两条已经 100 天没被提及，正常早就被忘了；带 pendingDigest 必须留下
        await org.organize(None, None, None)
        dl1b = read_layer(root, "DL1")
        assert_(len(dl1b) == 2,
                f"等模型整理的条目不该被遗忘，实际剩 {len(dl1b)} 条（被忘就是丢数据）")

        print("PASS A3：无模型时 L1→DL1（不碰 L2），pendingDigest 免于遗忘")
        print(f"  整理结果 {res}")
        print(f"  L1={len(l1)} L2={len(l2)} DL1={len(dl1)} → 二次整理后 DL1={len(dl1b)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
