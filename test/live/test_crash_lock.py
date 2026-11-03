"""突发情况：进程被强杀 + 抢锁。

场景：
  1. 另一个进程拿着 organize.lock → 这边 organize() 必须立刻返回 busy，不能硬等
  2. 持锁进程被 kill -9（Windows 上是 TerminateProcess）→ 锁文件留在盘上
  3. 超过 stale_lock_ms 后，新进程能接管、正常整理、并释放锁
  4. 全程原文条数不变（一条不丢）

用法：python astrbot/test/live/test_crash_lock.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, CORE)

from organizer import DATA_LAYERS, Organizer  # noqa: E402

HOLD = os.path.join(HERE, "_holdlock.py")


def write_layer(root: str, name: str, items: list[dict]) -> None:
    with open(os.path.join(root, f"{name}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": 2, "at": 0, "items": items}, f, ensure_ascii=False)


def main() -> None:
    root = tempfile.mkdtemp(prefix="crash-")
    try:
        items = [{"id": f"c{i}", "text": f"原文{i}", "layer": "L1",
                  "createdAt": 1762000000000 + i} for i in range(4)]
        write_layer(root, "L1", items)
        for name in ("L2", "DL1", "DL2", "DL3"):
            write_layer(root, name, [])
        lock = os.path.join(root, "organize.lock")

        # 1. 子进程持锁
        p = subprocess.Popen([sys.executable, HOLD, root, CORE],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = p.stdout.readline().strip() if p.stdout else ""
        assert "LOCKED" in line, f"持锁进程没起来：{line} / {p.stderr.read()[:200] if p.stderr else ''}"
        assert os.path.exists(lock), "应该留下 organize.lock"

        org = Organizer(root, "astrbot", {"stale_lock_ms": 600, "quiet_ms": 0})
        st = asyncio.run(org.organize(None, None, None))
        assert st.get("skipped") == "busy", f"别人持锁时应返回 busy，实际 {st}"
        print("PASS 1/4：锁被占用时立即返回 busy（不硬等）")

        # 2. 强杀
        p.kill()
        p.wait(timeout=10)
        assert os.path.exists(lock), "强杀后锁文件应残留（这正是要接管的情形）"
        print("PASS 2/4：kill -9 后锁文件残留")

        # 3. 超过 stale 后接管
        time.sleep(0.8)
        st2 = asyncio.run(org.organize(None, None, None))
        assert st2.get("ok"), f"stale 锁应能接管，实际 {st2}"
        assert not os.path.exists(lock), "整理结束后应释放锁"
        print("PASS 3/4：超过 stale_lock_ms 后接管成功并放锁")

        # 4. 数据完整（无模型 → L1 下沉 DL1，总数不变）
        db = org.load_db()
        total = sum(len(db[l]) for l in DATA_LAYERS)
        assert total == 4, f"原文条数应保持 4，实际 {total}"
        print(f"PASS 4/4：数据完整，层分布 {[(l, len(db[l])) for l in DATA_LAYERS]}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
