"""批次 4 回归：信号入口闭环（/dsm attend / pin / forget 的底层）。

`write_signal()` 只往 `<dir>/signals/<writer>.jsonl` 追加一行（与 dsh 的 `signal()`
同格式）；整理时 `apply_signals()` 认领并消费。这里验证：
  - attend：recallCount +1、DL1 升回 L2
  - pin / unpin：pinned 翻转
  - forget：条目从所有层消失，且台账里有记录
  - 同一个 opId 重放不会重复生效

`apply_once()` 模拟 organize() 的收尾顺序：先记 applied.json，再删掉已认领的
信号文件（顺序不能反，否则崩溃会丢信号）。

用法：python astrbot/test/test_signals_roundtrip.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "core")
sys.path.insert(0, CORE)

from organizer import DATA_LAYERS, Organizer, write_signal  # noqa: E402

NOW = int(time.time() * 1000)


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


def ids_in(db: dict) -> set[str]:
    return {x.get("id") for l in DATA_LAYERS for x in db[l]}


def apply_once(org: Organizer, db: dict, root: str) -> dict:
    """模拟 organize()：消费信号 → 记 applied → 删已认领文件。"""
    res = org.apply_signals(db)
    sig_dir = os.path.join(root, "signals")
    if res.get("applied"):
        with open(os.path.join(sig_dir, "applied.json"), "w", encoding="utf-8") as f:
            json.dump(res["applied"], f, ensure_ascii=False)
    for name in os.listdir(sig_dir):
        if ".claimed-" in name:
            try:
                os.unlink(os.path.join(sig_dir, name))
            except OSError:
                pass
    return res


def fresh() -> tuple[str, Organizer, dict]:
    root = tempfile.mkdtemp(prefix="signals-")
    org = Organizer(root, "astrbot", {})
    dl1 = {
        "id": "DL1-a", "text": "流水里的一条", "layer": "DL1",
        "createdAt": NOW - 86400000, "lastMentionedAt": NOW - 86400000,
        "heat": 1, "repeatStrength": 1, "recallCount": 0,
    }
    dl3 = {
        "id": "DL3-a", "text": "一条长期摘要", "kind": "summary", "layer": "DL3",
        "createdAt": NOW - 86400000, "heat": 2, "repeatStrength": 1,
        "importance": 0.9, "sources": [],
    }
    db = {"L1": [], "L2": [], "DL1": [dl1], "DL2": [], "DL3": [dl3]}
    return root, org, db


def main() -> None:
    # —— 场景 1：attend 升层 + opId 重放 ——
    root, org, db = fresh()
    try:
        rec = write_signal(root, "astrbot", "attend", ["DL1-a"])
        res = apply_once(org, db, root)
        dl1 = next(x for l in DATA_LAYERS for x in db[l] if x["id"] == "DL1-a")
        assert_(res["ops"] == 1, f"应消费 1 个 op，实际 {res}")
        assert_(dl1["recallCount"] == 1, f"recallCount 应 +1，实际 {dl1['recallCount']}")
        assert_(dl1["layer"] == "L2" and dl1 in db["L2"], "DL1 被关注后应升回 L2")

        # 同一条记录再投一次：applied.json 里已有该 opId，不能再生效
        with open(os.path.join(root, "signals", "astrbot.jsonl"), "a",
                  encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        res2 = apply_once(org, db, root)
        assert_(res2["ops"] == 0, f"同一 opId 不应重复生效，实际 {res2['ops']}")
        assert_(dl1["recallCount"] == 1, "重放不应再加热度")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # —— 场景 2：pin / unpin ——
    root, org, db = fresh()
    try:
        write_signal(root, "astrbot", "pin", ["DL3-a"], True)
        apply_once(org, db, root)
        dl3 = next(x for x in db["DL3"] if x["id"] == "DL3-a")
        assert_(dl3.get("pinned") is True, "pin 没生效")
        write_signal(root, "astrbot", "pin", ["DL3-a"], False)
        apply_once(org, db, root)
        assert_(dl3.get("pinned") is False, "unpin 没生效")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # —— 场景 3：forget 删条 + 写台账 ——
    root, org, db = fresh()
    try:
        write_signal(root, "astrbot", "forget", ["DL3-a"])
        n = apply_once(org, db, root)["ops"]
        assert_(n == 1, f"应消费 1 个遗忘 op，实际 {n}")
        assert_("DL3-a" not in ids_in(db), "forget 后条目仍在库里")

        grave = os.path.join(root, "graveyard", "forgotten.jsonl")
        assert_(os.path.exists(grave), f"遗忘应写台账：{grave}")
        with open(grave, "r", encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
        assert_(any(r.get("id") == "DL3-a" for r in rows), "台账里没有这条")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("PASS 批次 4：attend 升层 / opId 防重放 / pin 翻转 / forget 删条并写台账")


if __name__ == "__main__":
    main()
