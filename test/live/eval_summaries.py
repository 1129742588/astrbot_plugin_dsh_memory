"""摘要质量评估（客观指标 + 原文对照）。

不看"文笔"，只看能用指标衡量的东西：
  - sources：这条摘要引用了多少条原文（0 = 孤儿摘要）
  - 压缩比：摘要字数 / 被引用原文总字数
  - 数字保留：原文里的数字有多少进了摘要（决策、指标、数量）
  - 数字幻觉：摘要里出现、但原文里没有的数字（编造）
  - 术语保留：英文/型号类词（nnUNet、dice、markdown、CT…）的保留率

输出 `run/summary-quality.json`，控制台打印前几条摘要 + 对应原文，便于人工看。

用法：python astrbot/test/live/eval_summaries.py [库目录]
"""

from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, CORE)

from tiers import Tiers  # noqa: E402

RUN = os.path.join(HERE, "run")
DEFAULT_LIB = os.path.join(RUN, "astrbot-lib")

NUM = re.compile(r"\d+(?:\.\d+)?")
TERM = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{2,}")


def nums(text: str) -> set[str]:
    return set(NUM.findall(text or ""))


def terms(text: str) -> set[str]:
    return {t.lower() for t in TERM.findall(text or "")}


def main() -> None:
    lib = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LIB
    tiers = Tiers(lib, writer="astrbot")
    dl3 = tiers.read_layer_safe("DL3")
    pool = {}
    for name in ("DL2", "DL1", "L2"):
        for it in tiers.read_layer_safe(name):
            pool[it.get("id")] = it

    rows = []
    for s in dl3:
        srcs = [pool[i] for i in (s.get("sources") or []) if i in pool]
        src_text = "\n".join(x.get("text") or "" for x in srcs)
        sum_text = s.get("text") or ""
        sn, rn = nums(sum_text), nums(src_text)
        st, rt = terms(sum_text), terms(src_text)
        rows.append({
            "id": s.get("id"),
            "text": sum_text,
            "sources": len(srcs),
            "sum_chars": len(sum_text),
            "src_chars": len(src_text),
            "ratio": round(len(sum_text) / max(1, len(src_text)), 3),
            "num_kept": len(sn & rn),
            "num_src": len(rn),
            "num_halluc": sorted(sn - rn)[:5],
            "term_kept": len(st & rt),
            "term_src": len(rt),
            "importance": s.get("importance"),
        })

    total = len(rows)
    good_src = sum(1 for r in rows if r["sources"] > 0)
    halluc = sum(1 for r in rows if r["num_halluc"])
    keep = [r["num_kept"] / r["num_src"] for r in rows if r["num_src"]]
    term_keep = [r["term_kept"] / r["term_src"] for r in rows if r["term_src"]]
    ratios = [r["ratio"] for r in rows if r["src_chars"]]

    summary = {
        "lib": lib,
        "summaries": total,
        "with_sources": good_src,
        "num_recall": round(sum(keep) / len(keep), 3) if keep else None,
        "num_hallucination_rate": round(halluc / total, 3) if total else None,
        "term_recall": round(sum(term_keep) / len(term_keep), 3) if term_keep else None,
        "compress_ratio": round(sum(ratios) / len(ratios), 3) if ratios else None,
        "rows": rows,
    }
    out = os.path.join(RUN, "summary-quality.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)

    print(f"摘要 {total} 条｜有 sources {good_src} 条｜数字保留 "
          f"{summary['num_recall']}｜数字幻觉率 {summary['num_hallucination_rate']}｜"
          f"术语保留 {summary['term_recall']}｜平均压缩比 {summary['compress_ratio']}")
    for r in rows[:5]:
        print(f"\n- [{r['id']}] {r['sources']} 源 / {r['src_chars']}→{r['sum_chars']} 字"
              f"（{r['ratio']}）数字 {r['num_kept']}/{r['num_src']}"
              f" 幻觉 {r['num_halluc'] or '无'}")
        print(f"  摘要：{r['text'][:120]}")
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
