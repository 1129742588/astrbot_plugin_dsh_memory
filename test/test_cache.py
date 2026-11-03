"""批次 2 回归：只读层缓存 + 懒建 id 索引。

对齐 dsh 的 `readLayerCached` / `ensureIndex`：
  - key = (mtime_ns, size)：对方原子替换后自动失效，不需要显式通知
  - 单层超过上限不缓存（避免百万条吃满内存）
  - 返回的是同一个 list 对象（调用方只读，整理层走 load_db 直读）
  - id 索引懒建，跟缓存条目一起失效

用法：python astrbot/test/test_cache.py
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

from tiers import Tiers  # noqa: E402

N = 50000


def write_layer(root: str, name: str, items: list[dict]) -> None:
    with open(os.path.join(root, f"{name}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": 2, "at": 0, "items": items}, f, ensure_ascii=False)


def make_items(n: int) -> list[dict]:
    return [
        {
            "id": f"x{i}",
            "text": f"第{i}条记忆",
            "layer": "L2",
            "createdAt": 1762000000000 + i,
            "consolidatedAt": 1762000000000 + i,
            "origin": "astrbot",
            "speaker": "user",
            "people": [],
            "importance": 0.5,
            "heat": 1,
            "repeatStrength": 1,
        }
        for i in range(n)
    ]


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    root = tempfile.mkdtemp(prefix="cache-")
    try:
        items = make_items(N)
        write_layer(root, "L2", items)
        for name in ("L1", "L3", "DL1", "DL2", "DL3"):
            write_layer(root, name, [])

        tiers = Tiers(root, writer="astrbot")

        # —— 1. 第二次读盘几乎不耗时，且返回同一对象 ——
        t0 = time.perf_counter()
        a = tiers.read_layer_safe("L2")
        t1 = time.perf_counter()
        b = tiers.read_layer_safe("L2")
        t2 = time.perf_counter()
        assert_(len(a) == N, f"应读到 {N} 条，实际 {len(a)}")
        assert_(a is b, "缓存命中应返回同一个对象，不应该重新解析")
        first, second = t1 - t0, t2 - t1
        assert_(second < max(first * 0.2, 0.0005),
                f"第二次读应几乎不耗时：{first * 1000:.1f}ms → {second * 1000:.3f}ms")

        # —— 2. 外部写盘（模拟 dsh 原子替换）后自动失效 ——
        write_layer(root, "L2", items[:10])
        c = tiers.read_layer_safe("L2")
        assert_(len(c) == 10, f"外部写盘后应读到新内容，实际 {len(c)} 条")

        # —— 3. 超过上限不缓存 ——
        write_layer(root, "L2", items[:50])
        small = Tiers(root, writer="astrbot", layer_cache_max_items=10)
        small.read_layer_safe("L2")
        assert_("L2" not in small._cache, "超过上限的层不该进缓存")

        # —— 4. id 索引懒建，且与缓存同生命周期 ——
        write_layer(root, "L2", items)
        tiers.invalidate()
        tiers.read_layer_safe("L2")
        idx = tiers._index_of("L2")
        assert_(len(idx) == N, f"id 索引应有 {N} 条，实际 {len(idx)}")
        assert_(idx["x123"]["text"] == "第123条记忆", "索引取到的条目不对")
        assert_(tiers._index_of("L2") is idx, "索引应只建一次")

        # —— 5. 注入路径重复调用应显著变快（L2 + L3 + L1 都走缓存）——
        write_layer(root, "L1", make_items(200))
        block1 = tiers.build_block()
        tb0 = time.perf_counter()
        block2 = tiers.build_block()
        tb1 = time.perf_counter()
        assert_(block1 == block2, "同样的数据两次注入块应完全一致")
        assert_(tb1 - tb0 < 0.05, f"缓存命中后 build_block 应 < 50ms，实际 {(tb1 - tb0) * 1000:.1f}ms")
        assert_("L2" in tiers._sort_cache, "L2 排序结果应被缓存")

        # —— 6. L2 变了要重排（排序缓存按签名失效）——
        write_layer(root, "L2", list(reversed(make_items(100))))
        block3 = tiers.build_block()
        assert_(block3 != block2, "L2 内容变了，注入块应该跟着变（排序缓存没失效）")

        print(f"PASS 批次 2：{N} 条 L2 首读 {first * 1000:.0f}ms → 命中 {second * 1000:.3f}ms；"
              f"外部写盘可见、超限不缓存、id 索引可用")
        print(f"  build_block 第二次 {(tb1 - tb0) * 1000:.2f}ms，hit/miss="
              f"{tiers.cache_hits}/{tiers.cache_misses}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
