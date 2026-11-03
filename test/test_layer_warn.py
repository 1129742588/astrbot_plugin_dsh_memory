"""可选项回归：层容量告警。

单层超过 `warn_layer_items`（默认 20 万）时，整理会往 audit.log 写一条
`LAYER_WARN`。不改任何行为，只让"为什么这么慢 / 为什么整理不动"有据可查。

用法：python astrbot/test/test_layer_warn.py
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

from organizer import Organizer  # noqa: E402


def write_layer(root: str, name: str, items: list[dict]) -> None:
    with open(os.path.join(root, f"{name}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": 2, "at": 0, "items": items}, f, ensure_ascii=False)


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


async def main() -> None:
    root = tempfile.mkdtemp(prefix="warn-")
    try:
        write_layer(root, "L2", [
            {"id": f"b{i}", "text": f"缓冲{i}", "layer": "L2", "createdAt": 1762000000000 + i}
            for i in range(6)
        ])
        for name in ("L1", "DL1", "DL2", "DL3"):
            write_layer(root, name, [])

        org = Organizer(root, "astrbot", {"warn_layer_items": 5})
        await org.organize(None, None, None)

        with open(org.audit_path, "r", encoding="utf-8") as f:
            log = f.read()
        assert_("LAYER_WARN" in log, f"超过阈值应写 LAYER_WARN，实际日志：\n{log[-400:]}")
        assert_("L2=6" in log, f"告警里应带层与条数，实际：\n{log[-400:]}")
        assert_("阈值 5" in log, "告警里应带阈值")

        # 没超阈值时不该写（清空日志后换一个更高的阈值再整理一次）
        with open(org.audit_path, "w", encoding="utf-8"):
            pass
        org2 = Organizer(root, "astrbot", {"warn_layer_items": 100})
        await org2.organize(None, None, None)
        with open(org2.audit_path, "r", encoding="utf-8") as f:
            log2 = f.read()
        assert_("LAYER_WARN" not in log2, "未超阈值不该告警")

        print("PASS 可选项：单层超阈值写 LAYER_WARN，未超不写")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
