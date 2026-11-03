"""两端对照结果的差异分析（只读）。

读 astrbot/test/out/astrbot.json 与 dsh/test/out/dsh.json，
逐字段比出「一致 / 已知差异 / 意外差异」，把差异落到具体行或 id 上。

退出码：0 = 只有已知差异；1 = 出现未登记的差异（用于回归时拦一道）。

用法：python astrbot/test/compare_parity.py
"""

from __future__ import annotations

import difflib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
A_PATH = os.path.join(HERE, "out", "astrbot.json")
D_PATH = os.path.join(os.path.dirname(HERE), "..", "dsh", "test", "out", "dsh.json")

# 已登记的两端差异（审计报告 A/B 类编号 → 说明）
KNOWN = {
    "l1_view": "A9：astrbot 跳过 L1.tmp/*.claimed-*，dsh 的 readTempsAll 不跳过",
    "pick_ids": "B5：astrbot 的 item_target 用 maxItems/2（浮点），dsh 用 ceil",
    "cosine_unequal": "B6：astrbot 用 min(len) 截断后照算，dsh 长度不等直接返回 0",
    "make_item_people": "A7：astrbot 排除 speaker∈{user,bot}，dsh 把 speaker 并进 people",
    "block": "A9：astrbot 跳过 L1.tmp/*.claimed-*，注入块里少这一行（B4 已修，L3 不再少条）",
}


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# block 里允许被 astrbot 少掉的行（前缀匹配）：
#   「长摘要」= B4（L3 末尾少一条）
#   「已认领」= A9（.claimed- 条目不进 astrbot 的 L1 视图）
BLOCK_ALLOWED_MISSING = ("长摘要", "已认领")


def report_block(a: str, d: str) -> list[str]:
    al, dl = a.split("\n"), d.split("\n")
    out = []
    for line in difflib.unified_diff(dl, al, fromfile="dsh", tofile="astrbot", lineterm="", n=1):
        out.append(line)
    return out


def main() -> int:
    a, d = load(A_PATH), load(D_PATH)
    problems: list[str] = []
    print("=" * 64)
    print("两端对照结果（astrbot ← → dsh）")
    print("=" * 64)

    # 1) 注入块：唯一允许的差异是 B4（L3 末尾少一条）
    if a["block"] == d["block"]:
        print("[SAMELINE] block：逐字一致")
    else:
        diff = report_block(a["block"], d["block"])
        print(f"[DIFF] block：{KNOWN['block']}")
        for line in diff:
            print("        " + line)
        removed = [ln for ln in diff if ln.startswith("-") and not ln.startswith("---")]
        added = [ln for ln in diff if ln.startswith("+") and not ln.startswith("+++")]
        unexpected = [
            ln for ln in removed
            if not any(k in ln for k in BLOCK_ALLOWED_MISSING)
        ]
        if unexpected or added:
            problems.append(
                f"block 出现未预期的增删（unexpected={len(unexpected)} added={len(added)}）"
            )

    # 2) L1 视图
    aid, did = a["l1_view_ids"], d["l1_view_ids"]
    if aid == did:
        print("[SAMELINE] l1_view_ids：一致")
    else:
        only_d = [i for i in did if i not in aid]
        only_a = [i for i in aid if i not in did]
        print(f"[DIFF] l1_view_ids：{KNOWN['l1_view']}")
        print(f"        仅 dsh 有：{only_d}   仅 astrbot 有：{only_a}")
        if only_a or len(only_d) > 1:
            problems.append(f"l1_view 差异超出 .claimed-（only_d={only_d} only_a={only_a}）")

    # 3) 按轮取批
    if a["pick_ids"] == d["pick_ids"]:
        print("[SAMELINE] pick_ids：一致")
    else:
        print(f"[DIFF] pick_ids：{KNOWN['pick_ids']}")
        print(f"        astrbot={a['pick_ids']}")
        print(f"        dsh    ={d['pick_ids']}")
        if not set(d["pick_ids"]).issubset(set(a["pick_ids"])) or \
                len(a["pick_ids"]) - len(d["pick_ids"]) != 1:
            problems.append("pick 差异不是「astrbot 多取 1 条」这一种")

    # 4) 余弦
    print(f"[{'SAMELINE' if a['cosine_unequal'] == d['cosine_unequal'] else 'DIFF'}] "
          f"cosine_unequal：astrbot={a['cosine_unequal']} dsh={d['cosine_unequal']}")
    if a["cosine_unequal"] != d["cosine_unequal"]:
        print(f"        {KNOWN['cosine_unequal']}")
        if not (a["cosine_unequal"] > 0 and d["cosine_unequal"] == 0):
            problems.append("余弦差异方向不对（预期 astrbot>0、dsh=0）")

    # 5) people
    print(f"[{'SAMELINE' if a['make_item_people'] == d['make_item_people'] else 'DIFF'}] "
          f"make_item_people：astrbot={a['make_item_people']} dsh={d['make_item_people']}")
    if a["make_item_people"] != d["make_item_people"]:
        print(f"        {KNOWN['make_item_people']}")
        if a["make_item_people"] or d["make_item_people"] != ["user"]:
            problems.append("people 差异形态与登记不符")

    # 6) 检索（这条必须完全一致，任何差异都算意外）
    if a["search_hits"] == d["search_hits"] and a["search_depth"] == d["search_depth"]:
        print(f"[SAMELINE] search：depth={a['search_depth']} hits="
              f"{[h['id'] for h in a['search_hits']]} 完全一致")
    else:
        print("[DIFF] search：未登记差异（应完全一致）")
        print(f"        astrbot={a['search_hits']}")
        print(f"        dsh    ={d['search_hits']}")
        problems.append("search 结果不一致（两端打分/下探逻辑出现偏差）")

    print("-" * 64)
    if problems:
        print(f"结论：发现 {len(problems)} 处未登记差异")
        for p in problems:
            print("  ✗ " + p)
        return 1
    print("结论：全部差异都在已登记清单内，无新增偏差")
    return 0


if __name__ == "__main__":
    sys.exit(main())
