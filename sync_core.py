#!/usr/bin/env python3
"""把 core/ 里的源码同步到插件根目录。

为什么会有两份：
    插件市场（以及直接丢进 data/plugins/ 的安装方式）要求 main.py / metadata.yaml
    / _conf_schema.json 就在仓库根目录；但源码真源统一放在 core/，两端结构一致。

约定：
    真源 = core/        改代码只改这里
    副本 = 根目录       由本脚本生成，别手改（改了也会被覆盖）

用法：
    python sync_core.py            # core -> 根目录
    python sync_core.py --check    # 只检查是否一致，不一致退出码 1
"""

from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE / "core"
SUFFIXES = {".py", ".json", ".yaml", ".yml"}
SKIP = {"sync_core.py"}


def core_files() -> list[Path]:
    return sorted(
        p for p in CORE.iterdir() if p.is_file() and p.suffix in SUFFIXES and p.name not in SKIP
    )


def main() -> int:
    check = "--check" in sys.argv
    if not CORE.is_dir():
        print(f"找不到 {CORE}")
        return 2

    stale, same = [], []
    for src in core_files():
        dst = HERE / src.name
        if dst.exists() and filecmp.cmp(src, dst, shallow=False):
            same.append(src.name)
            continue
        stale.append(src.name)
        if not check:
            shutil.copy2(src, dst)

    if check:
        if stale:
            print("根目录副本与 core/ 不一致：" + ", ".join(stale))
            print("跑一次 python sync_core.py 即可。")
            return 1
        print(f"一致：{len(same)} 个文件")
        return 0

    print(f"已同步 {len(stale)} 个：{', '.join(stale) if stale else '（无变化）'}")
    print(f"未变化 {len(same)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
