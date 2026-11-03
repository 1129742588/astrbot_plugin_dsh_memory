"""两端提示词逐行对照（防止"改了一端忘了另一端"）。

覆盖：
  1. 分类（单条）system
  2. 分类（批量）system
  3. 判关系 system
  4. 摘要 user 的固定文案行（含动态值的行跳过）

dsh 侧不 import（prompt 是 silicon.mjs 的模块内私有常量），用正则从源码取数组字面量。

用法：python astrbot/test/compare_prompts.py
退出码：0 = 全部一致；1 = 有偏差（并打印第一处不同）
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "core")
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, CORE)

from organizer import (  # noqa: E402
    CLASSIFY_BATCH_SYSTEM_PROMPT,
    CLASSIFY_SYSTEM_PROMPT,
    RELATION_SYSTEM_PROMPT,
    build_summary_prompt,
    raw_line,
)

SILICON = os.path.join(ROOT, "dsh", "silicon.mjs")
SRC = open(SILICON, encoding="utf-8", newline="").read().replace("\r\n", "\n")


def take_array(name: str, src: str = SRC) -> str:
    """取 `const NAME = [ ... ].join('\\n')` 并按 JS 语义拼成一段文本。"""
    pat = re.compile(rf"const\s+{name}\s*=\s*\[(.*?)\]\s*\.join\('\\n'\)", re.S)
    m = pat.search(src)
    if not m:
        raise SystemExit(f"dsh 源码里找不到 {name}")
    body = m.group(1)
    # JS 里 'a' + 'b' 是一条元素，join 后是一行；先把相邻拼接合并，
    # 否则提取器会把它们当成两行（实际运行时并不换行）。
    body = re.sub(r"'\s*\+\s*'", "", body)
    parts = re.findall(r"'((?:[^'\\]|\\.)*)'", body)
    out = []
    for p in parts:
        out.append(p.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
                   if "\\" in p else p)
    return "\n".join(out)


def take_in_fn(fn: str, name: str) -> str:
    i = SRC.find(f"function {fn}")
    if i < 0:
        raise SystemExit(f"dsh 源码里找不到函数 {fn}")
    return take_array(name, SRC[i : i + 20000])


def diff_lines(a: str, b: str, title: str) -> list[str]:
    la, lb = a.split("\n"), b.split("\n")
    if la == lb:
        print(f"[SAMELINE] {title}（{len(la)} 行）")
        return []
    print(f"[DIFF] {title}")
    problems = []
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<缺>"
        y = lb[i] if i < len(lb) else "<缺>"
        if x != y:
            print(f"  第 {i + 1} 行：")
            print(f"    astrbot: {x!r}")
            print(f"    dsh    : {y!r}")
            problems.append(f"{title} 第 {i + 1} 行不同")
            if len(problems) >= 3:
                print("  …（只列前三处）")
                break
    return problems


def main() -> None:
    problems: list[str] = []

    problems += diff_lines(CLASSIFY_SYSTEM_PROMPT, take_array("CLASSIFY_SYS"), "分类（单条）")
    problems += diff_lines(
        CLASSIFY_BATCH_SYSTEM_PROMPT, take_array("CLASSIFY_BATCH_SYS"), "分类（批量）"
    )
    problems += diff_lines(RELATION_SYSTEM_PROMPT, take_in_fn("relation", "sys"), "判关系")

    # 摘要 user：dsh 的模板里混着三元分支（``这批里…`` / ``原文里如果…``），
    # 按"行"硬比会全是噪音。改用「astrbot 的每条固定文案都必须出现在 dsh 模板里」，
    # 这样改了一端忘了另一端时一定报错，而分支差异不会误报。
    _, user_p = build_summary_prompt(
        [raw_line({"createdAt": 1761740800000, "speaker": "user", "text": "样例原文"})],
        "dialog",
        ["样例人"],
    )
    a_lines = [
        ln
        for ln in user_p.split("\n")
        if ln
        and len(ln) > 4
        and not ln.startswith(("1. ", "原文："))
        and "这一批共" not in ln
        and "条的摘要会被直接丢弃" not in ln   # dsh 侧是 `${maxLines}` 模板
        and not ln.startswith("这批里出现过的人")
        and not ln.startswith("重点：")   # dsh 侧是 focus 变量，展开比没有意义
    ]
    d_tpl = take_in_fn("summarizeWithSources", "user")
    missing = [ln for ln in a_lines if ln not in d_tpl]
    if missing:
        print(f"[DIFF] 摘要 user：astrbot 有 {len(missing)} 行在 dsh 模板里找不到")
        for ln in missing[:5]:
            print(f"    {ln!r}")
        problems += [f"摘要 user 缺 {len(missing)} 行"]
    else:
        print(f"[SAMELINE] 摘要 user 固定文案：{len(a_lines)} 行全部出现在 dsh 模板里")

    print("-" * 60)
    if problems:
        print(f"结论：{len(problems)} 处不一致")
        sys.exit(1)
    print("结论：两端提示词逐行一致")


if __name__ == "__main__":
    main()
