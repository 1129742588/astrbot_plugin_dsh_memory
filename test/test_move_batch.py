"""批次 3 回归：批量搬层（10 万条不再 O(N²)）。

原实现每条 `_move` 都要扫 5 个层并重建 list：10 万条 = 35 分钟。
现在按目标层分桶后一次搬，O(5×层大小 + N)。

语义必须与 `_move` 完全一致：
  - 搬到目标层后，其它层里不再有同一个 id
  - 同层再搬是幂等的（不会重复 append）
  - 进 L2 要记 consolidatedAt

用法：python astrbot/test/test_move_batch.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "core")
sys.path.insert(0, CORE)

from organizer import Organizer  # noqa: E402

N = 100_000


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    root = tempfile.mkdtemp(prefix="move-batch-")
    try:
        org = Organizer(root, "astrbot", {})
        items = [{"id": f"m{i}", "text": f"第{i}条", "layer": "L1"} for i in range(N)]
        db = {"L1": list(items), "L2": [], "DL1": [], "DL2": [], "DL3": []}

        # —— 1. 10 万条 L1 → DL1 ——
        t0 = time.perf_counter()
        org._move_many(db, items, "DL1", 111)
        dt = time.perf_counter() - t0
        assert_(len(db["DL1"]) == N, f"DL1 应有 {N} 条，实际 {len(db['DL1'])}")
        assert_(len(db["L1"]) == 0, f"L1 应清空，实际 {len(db['L1'])}")
        assert_(all(it["layer"] == "DL1" for it in db["DL1"]), "layer 字段没更新")
        assert_(dt < 1.0, f"10 万条搬层应 < 1s，实际 {dt:.2f}s")

        # —— 2. DL1 → L2：要记 consolidatedAt ——
        org._move_many(db, items, "L2", 222)
        assert_(len(db["L2"]) == N and len(db["DL1"]) == 0, "搬层后两层条数不对")
        assert_(all(it.get("consolidatedAt") == 222 for it in db["L2"]),
                "进 L2 必须写 consolidatedAt")

        # —— 3. 同层再搬：幂等，不能重复 append ——
        org._move_many(db, items, "L2", 333)
        assert_(len(db["L2"]) == N, f"同层搬应幂等，实际 {len(db['L2'])} 条")

        # —— 4. 与逐条 _move 的结果一致（小样本对照）——
        small = [{"id": f"s{i}", "text": "x", "layer": "L1"} for i in range(20)]
        db_a = {"L1": list(small), "L2": [], "DL1": [], "DL2": [], "DL3": []}
        db_b = {"L1": list(small), "L2": [], "DL1": [], "DL2": [], "DL3": []}
        for it in small:
            org._move(db_b, it, "DL2", 444)
        org._move_many(db_a, small, "DL2", 444)
        for layer in ("L1", "L2", "DL1", "DL2", "DL3"):
            ids_a = [x["id"] for x in db_a[layer]]
            ids_b = [x["id"] for x in db_b[layer]]
            assert_(ids_a == ids_b, f"{layer} 批量与逐条结果不一致：{ids_a[:3]} vs {ids_b[:3]}")

        print(f"PASS 批次 3：10 万条搬层 {dt * 1000:.0f}ms（< 1s），"
              f"同层幂等、L2 记 consolidatedAt、与逐条 _move 结果一致")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
