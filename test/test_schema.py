#!/usr/bin/env python3
"""配置页自检：`_conf_schema.json` 和 `main.DEFAULTS` 必须一一对上。

配置页最容易出的毛病是"界面上有、代码里没有"（填了不起作用）或者
"代码里有、界面上没有"（永远只能用默认值）。这里把两组键对撞一遍，
再顺手查默认值和类型。

跑法：python astrbot/test/test_schema.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "core")
sys.path.insert(0, CORE)

SCHEMA_PATH = os.path.join(CORE, "_conf_schema.json")
SCHEMA = json.load(open(SCHEMA_PATH, encoding="utf-8"))

TYPE_MAP = {
    "bool": bool,
    "int": int,
    "float": (int, float),
    "string": str,
    "text": str,
}


def leaves() -> dict[str, dict]:
    """摊平成 {键: 字段定义}，同时检查分组结构合法。"""
    out: dict[str, dict] = {}
    for gname, group in SCHEMA.items():
        assert not gname.startswith("_"), f"分组名不该以下划线开头：{gname}"
        assert group.get("type") == "object", f"分组 {gname} 必须是 type=object"
        assert "description" in group, f"分组 {gname} 缺 description（配置页会显示空标题）"
        items = group.get("items")
        assert isinstance(items, dict) and items, f"分组 {gname} 缺 items"
        for key, spec in items.items():
            assert not key.startswith("_"), f"残留的伪字段 {key}（旧版 _head_* 分隔头已被分组替代）"
            assert key not in out, f"键 {key} 在多个分组里重复出现"
            out[key] = spec
    return out


def check_flatten() -> list[str]:
    """分组配置必须能被 flatten_cfg 摊平回一维，且老格式仍然认。"""
    src = open(os.path.join(CORE, "main.py"), encoding="utf-8").read()
    start = src.index("def flatten_cfg(")
    end = src.index("\ndef ", start + 10)
    ns: dict = {"Any": object}
    exec(src[start:end], ns)  # noqa: S102
    flatten = ns["flatten_cfg"]

    errs: list[str] = []
    nested = {g: {k: s["default"] for k, s in spec["items"].items()} for g, spec in SCHEMA.items()}
    flat = flatten(nested)
    for g, spec in SCHEMA.items():
        for key in spec["items"]:
            if key not in flat:
                errs.append(f"flatten 后丢了 {g}.{key}")
    if flatten({"_head_dir": "--- 数据目录 ---", "dir": "/x"}) != {"dir": "/x"}:
        errs.append("flatten 没有过滤掉旧版的 _head_* 伪字段")
    if flatten({"basic": {"dir": "/a"}, "dir": "/b"}) != {"dir": "/b"}:
        errs.append("flatten 的顶层键应该覆盖分组里的同名键")
    return errs


def main() -> int:
    sys.path.insert(0, CORE)
    # 只取 DEFAULTS，不 import 整个插件（那里会拉 astrbot 的 api）
    src = open(os.path.join(CORE, "main.py"), encoding="utf-8").read()
    start = src.index("DEFAULTS = {")
    end = src.index("}", start) + 1
    ns: dict = {}
    exec(src[start:end], ns)  # noqa: S102 - 只求值一个纯字面量字典
    defaults: dict = ns["DEFAULTS"]

    fields = leaves()
    errs: list[str] = []

    # 1) 键集合一致
    only_ui = sorted(set(fields) - set(defaults))
    only_code = sorted(set(defaults) - set(fields))
    if only_ui:
        errs.append(f"界面上有、代码里没有：{only_ui}")
    if only_code:
        errs.append(f"代码里有、界面上没有：{only_code}")

    # 2) 类型与默认值
    for key, spec in fields.items():
        if key not in defaults:
            continue
        typ = spec.get("type")
        if typ not in TYPE_MAP:
            errs.append(f"{key}: 未知类型 {typ}")
            continue
        if not isinstance(defaults[key], TYPE_MAP[typ]):
            errs.append(
                f"{key}: schema 写 {typ}，DEFAULTS 是 {type(defaults[key]).__name__}"
            )
        if "default" not in spec:
            errs.append(f"{key}: 缺 default")
        elif spec["default"] != defaults[key]:
            errs.append(f"{key}: default {spec['default']!r} != DEFAULTS {defaults[key]!r}")
        if "description" not in spec:
            errs.append(f"{key}: 缺 description")
        opts = spec.get("options")
        if opts is not None:
            if spec.get("default") not in opts:
                errs.append(f"{key}: default 不在 options 里")
            if not all(isinstance(o, str) for o in opts):
                errs.append(f"{key}: options 必须全是字符串")
        slid = spec.get("slider")
        if slid is not None:
            if typ not in ("int", "float"):
                errs.append(f"{key}: 只有 int/float 能配 slider")
            else:
                lo, hi, st = slid.get("min"), slid.get("max"), slid.get("step")
                if lo is None or hi is None or st is None:
                    errs.append(f"{key}: slider 缺 min/max/step")
                elif not lo <= spec.get("default", lo) <= hi:
                    errs.append(f"{key}: default 不在 slider 范围 {lo}~{hi}")
                elif st <= 0:
                    errs.append(f"{key}: slider step 必须 > 0")

    # 3) 取值范围（0~1 的比例、非负的容量）
    for key in ("hot_quota_own", "forget_threshold", "semantic_min_sim"):
        if key in defaults and not 0 <= defaults[key] <= 1:
            errs.append(f"{key}: 默认 {defaults[key]} 不在 0~1")
    for key in (
        "hot_max_chars",
        "long_max_chars",
        "consolidate_max_chars",
        "capture_user_chars",
        "capture_bot_chars",
        "organize_every_min",
        "quiet_min",
        "l2_keep_max",
        "summary_chunk",
        "embed_chunk",
        "semantic_top_k",
        "warn_layer_items",
    ):
        if key in defaults and defaults[key] <= 0:
            errs.append(f"{key}: 默认 {defaults[key]} 必须 > 0")

    # 4) 用户之前建过的分组不能丢
    want = {"basic", "inject", "capture", "organize", "semantic", "advanced"}
    missing = want - set(SCHEMA)
    if missing:
        errs.append(f"缺分组：{sorted(missing)}")

    # 5) 分组配置能被摊平回一维
    errs.extend(check_flatten())

    if errs:
        print("配置页自检失败：")
        for e in errs:
            print("  ✗", e)
        return 1

    print(f"配置页自检通过：{len(SCHEMA)} 组 / {len(fields)} 项")
    for gname, group in SCHEMA.items():
        print(f"  {gname:<9} {len(group['items'])} 项  {group['description']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
