"""批次 3 回归：超时检查进到 digest 内部。

原先 `_check_timeout` 只在阶段之间调用。模型卡在 digest 里时，整理协程会一直
占着 organize.lock（心跳还在续），别人既不能接管、也不会超时退出。

现在每片前查一次、路由每 2000 条查一次。这里用 2 个分组 + 慢模型验证：
第二个分组开始前就应该抛超时，并且锁被释放。

用法：python astrbot/test/test_digest_timeout.py
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

from organizer import Organizer  # noqa: E402


def write_layer(root: str, name: str, items: list[dict]) -> None:
    with open(os.path.join(root, f"{name}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": 2, "at": 0, "items": items}, f, ensure_ascii=False)


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


async def main() -> None:
    root = tempfile.mkdtemp(prefix="timeout-")
    try:
        old = int(time.time() * 1000) - 10000
        items = [
            {"id": "a1", "text": "场景一的原文", "speaker": "user", "scene": "群一",
             "channel": "dialog", "layer": "L1", "origin": "astrbot",
             "createdAt": old, "turnId": "t1"},
            {"id": "a2", "text": "场景二的原文", "speaker": "user", "scene": "群二",
             "channel": "dialog", "layer": "L1", "origin": "astrbot",
             "createdAt": old, "turnId": "t2"},
        ]
        write_layer(root, "L1", items)
        for name in ("L2", "DL1", "DL2", "DL3"):
            write_layer(root, name, [])

        org = Organizer(root, "astrbot", {
            "hot_max_chars": 1,          # 立即触发 L1 整理
            "hot_max_items": 10,
            "quiet_ms": 0,
            "max_organize_ms": 50,       # 50ms 就超时
        })

        calls = 0

        async def slow_summarize(lines, channel, folks, hint):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.15)    # 第一个分组就会用掉 150ms
            return "★ 摘要《1》\n"

        t0 = time.perf_counter()
        res = await org.organize(slow_summarize, None, None)
        dt = time.perf_counter() - t0

        assert_(dt < 2.0, f"超时后应尽快返回，实际 {dt:.2f}s")
        assert_(not res.get("ok"), f"超时不应返回 ok，实际 {res}")
        assert_("超时" in str(res.get("error") or ""), f"应报超时，实际 {res.get('error')}")
        assert_(calls == 1, f"第二个分组前就该退出，模型实际被调用 {calls} 次")

        lock = os.path.join(root, "organize.lock")
        assert_(not os.path.exists(lock), "超时退出后必须释放 organize.lock")

        print(f"PASS 批次 3：digest 内部超时生效（{dt * 1000:.0f}ms 退出，模型只调 1 次，锁已释放）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
