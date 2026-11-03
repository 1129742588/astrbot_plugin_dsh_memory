"""融合次数 / 关注次数的衰减回归（对应 dsh/test/test_strength_decay.mjs）。

背景（2026-11-02）：
  `fusionCount` / `recallCount` 原来是裸计数器，只增不减，也**不参与任何计算**——
  DL3 进上下文的排序只看 `importance × heat`。结果是：
    一条一年前被重复过 20 次的摘要，永远压过昨天刚被关注 3 次的那条。

约定：
  - 两个计数的**原始值**保留（台账、可审计）
  - 参与计算的是**衰减后的有效值**：`count × 0.5^(t/半衰期)`，
    等价于艾宾浩斯 R = e^(-t/S)（S = 半衰期/ln2）
  - 半衰期由 `repeatStrength` 决定（被重复得越多，忘得越慢）
  - DL3 排序分：`importance × heat × (1 + 0.25·log2(1+融合) + 0.35·log2(1+关注))`

用法：python astrbot/test/test_strength_decay.py
"""

from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "core"))

from tiers import DAY, effective_count, effective_strength, half_life_of, l3_score, memory_strength  # noqa: E402
from organizer import touch_heat  # noqa: E402


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    now = int(time.time() * 1000)

    # 半衰期：repeatStrength=1 → 10 × (1 + 0.3·log2(2)) = 13 天
    base = {"importance": 0.7, "heat": 1.0, "repeatStrength": 1.0}

    old_fusion = {**base, "fusionCount": 10, "lastFusedAt": now - 60 * DAY, "createdAt": now - 60 * DAY}
    new_fusion = {**base, "fusionCount": 2, "lastFusedAt": now, "createdAt": now}
    old_recall = {**base, "recallCount": 8, "lastRecalledAt": now - 90 * DAY, "createdAt": now - 90 * DAY}
    new_recall = {**base, "recallCount": 1, "lastRecalledAt": now, "createdAt": now}

    f_old = effective_count(old_fusion, "fusionCount", "lastFusedAt", now)
    f_new = effective_count(new_fusion, "fusionCount", "lastFusedAt", now)
    r_old = effective_count(old_recall, "recallCount", "lastRecalledAt", now)
    r_new = effective_count(new_recall, "recallCount", "lastRecalledAt", now)

    assert_(f_old < 0.6, f"60 天前融合 10 次，有效值应衰减到 1 以下，实际 {f_old:.3f}")
    assert_(f_new == 2.0, f"今天融合 2 次，有效值应正好 2，实际 {f_new}")
    assert_(f_new > f_old, "今天融合 2 次 应高于 60 天前融合 10 次")
    assert_(r_old < 0.2, f"90 天前关注 8 次，有效值应远小于 1，实际 {r_old:.3f}")
    assert_(r_new == 1.0, f"今天关注 1 次，有效值应为 1，实际 {r_new}")
    assert_(r_new > r_old, "今天关注 1 次 应高于 90 天前关注 8 次")
    print(f"PASS 1 衰减：融合 60 天前×10 → {f_old:.3f}；今天×2 → {f_new:.1f}；"
          f"关注 90 天前×8 → {r_old:.3f}；今天×1 → {r_new:.1f}")

    # 短间隔衰减很轻（连续衰减，不按天取整）：1 小时 ≈ 0.2%
    same_day = {**base, "fusionCount": 3, "lastFusedAt": now - 3600 * 1000, "createdAt": now}
    v = effective_count(same_day, "fusionCount", "lastFusedAt", now)
    assert_(2.99 < v < 3.0, f"1 小时前的 3 次融合应仍接近 3，实际 {v:.4f}")
    # 正好衰减一半时，有效值必须正好是一半
    half_life_at = {**base, "fusionCount": 6, "lastFusedAt": now - 13 * DAY, "createdAt": now - 13 * DAY}
    hv = effective_count(half_life_at, "fusionCount", "lastFusedAt", now)
    assert_(abs(hv - 3.0) < 1e-6, f"过一个半衰期应正好减半（6→3），实际 {hv:.4f}")
    print(f"PASS 2 短间隔≈不衰减（1 小时 {v:.4f}），过一个半衰期正好减半（6→{hv:.3f}）")

    # 排序：DL3 排序分要让"最近被用过的"排前面
    s_old_f = l3_score(old_fusion, now)
    s_new_f = l3_score(new_fusion, now)
    s_old_r = l3_score(old_recall, now)
    s_new_r = l3_score(new_recall, now)
    assert_(s_new_f > s_old_f, f"排序分：今天融合 2 次({s_new_f:.3f}) 应高于 60 天前 10 次({s_old_f:.3f})")
    assert_(s_new_r > s_old_r, f"排序分：今天关注 1 次({s_new_r:.3f}) 应高于 90 天前 8 次({s_old_r:.3f})")
    print(f"PASS 3 DL3 排序：新融合 {s_new_f:.3f} > 老融合 {s_old_f:.3f}；"
          f"新关注 {s_new_r:.3f} > 老关注 {s_old_r:.3f}")

    # 没有任何计数时，加成必须是 1（不能凭空抬高分数）
    plain = {**base, "fusionCount": 0, "recallCount": 0, "createdAt": now}
    plain_score = l3_score(plain, now)
    assert_(abs(plain_score - 0.7) < 1e-6,
            f"没有融合/关注时排序分应就是 importance×heat=0.7，实际 {plain_score:.6f}")
    print(f"PASS 4 无计数不加成：{plain_score:.3f}（= importance 0.7 × heat 1.0）")

    # 半衰期随 repeatStrength 变长（被重复得多 → 忘得慢）
    weak = {**base, "repeatStrength": 1.0, "fusionCount": 5, "lastFusedAt": now - 30 * DAY}
    strong = {**base, "repeatStrength": 15.0, "fusionCount": 5, "lastFusedAt": now - 30 * DAY}
    w = effective_count(weak, "fusionCount", "lastFusedAt", now)
    st = effective_count(strong, "fusionCount", "lastFusedAt", now)
    assert_(st > w, f"repeatStrength 高的应衰减更慢（{st:.3f} > {w:.3f}）")
    print(f"PASS 5 间隔效应：同样 30 天，strength=1 → {w:.3f}，strength=15 → {st:.3f}")

    # 老数据没有 lastFusedAt 时退回 lastMentionedAt / createdAt
    legacy = {**base, "fusionCount": 4, "createdAt": now - 30 * DAY}
    lv = effective_count(legacy, "fusionCount", "lastFusedAt", now)
    assert_(0 < lv < 4, f"缺 lastFusedAt 应退回 createdAt 计算，实际 {lv:.3f}")
    print(f"PASS 6 老数据兼容：缺 lastFusedAt 时按 createdAt 算得 {lv:.3f}")

    # 两个指标独立：只融合不关注 → recall 强度必须是 0
    fus, rec = memory_strength(new_fusion, now)
    assert_(fus == 2.0 and rec == 0.0, f"两个指标应独立，实际 fusion={fus} recall={rec}")
    print("PASS 7 指标独立：融合强度 2.0 / 关注强度 0.0")

    # ---------- 8. 强度本身会回落：久不提 → 半衰期变短 → 衰减更快 ----------
    t0 = now - 200 * DAY
    it = {"importance": 0.5, "heat": 1.0, "createdAt": t0, "lastMentionedAt": t0,
          "repeatStrength": 1.0}
    # 模拟「按 1/6/19/40/100 的间隔被提起」——touch_heat 会自然累积强度
    for d in (1, 6, 19, 40, 100):
        # 不能先手动改 lastMentionedAt，否则会被"同一天只算一次"短路
        touch_heat(it, t0 + d * DAY, {})
    raw = float(it["repeatStrength"])
    e_now = effective_strength({**it, "lastMentionedAt": now}, now)
    e_30 = effective_strength({**it, "lastMentionedAt": now - 30 * DAY}, now)
    e_120 = effective_strength({**it, "lastMentionedAt": now - 120 * DAY}, now)
    assert_(abs(e_now - raw) < 1e-9, f"刚提过时应等于原始强度 {raw}，实际 {e_now}")
    assert_(e_30 < e_now and e_120 < e_30, f"越久不提强度应越低：{e_now:.2f} > {e_30:.2f} > {e_120:.2f}")
    assert_(abs(e_120 - 1) < raw * 0.15, f"120 天不提应基本回到基线 1，实际 {e_120:.2f}")
    h_now = half_life_of({**it, "lastMentionedAt": now}, None, now)
    h_120 = half_life_of({**it, "lastMentionedAt": now - 120 * DAY}, None, now)
    assert_(h_120 < h_now, f"久不提后半衰期应变短（衰减更快）：{h_now:.1f} → {h_120:.1f}")
    print(f"PASS 8 强度回落：raw={raw:.0f} 刚提过 {e_now:.1f} / 30 天 {e_30:.1f} / 120 天 {e_120:.1f}；"
          f"半衰期 {h_now:.1f} → {h_120:.1f} 天")

    # ---------- 9. 间隔重复 > 集中爆发 ----------
    # A：第 0/1/6/19/40/100 天各提一次（间隔递增）
    # B：第 0 天一口气提 6 次（同一天只算一次）
    a = {"importance": 0.5, "heat": 1.0, "createdAt": t0, "lastMentionedAt": t0}
    for d in (0, 1, 6, 19, 40, 100):
        touch_heat(a, t0 + d * DAY, {})
    # B 的 6 次必须落在同一个自然日内（否则会跨天、正常加分），
    # 所以从"当天中午"开始，每次间隔 10 分钟
    import datetime

    noon = int(
        datetime.datetime.fromtimestamp(t0 / 1000)
        .replace(hour=12, minute=0, second=0, microsecond=0)
        .timestamp()
        * 1000
    )
    b = {"importance": 0.5, "heat": 1.0, "createdAt": noon, "lastMentionedAt": noon,
         "repeatStrength": 1.0}
    for k in range(6):
        touch_heat(b, noon + k * 10 * 60 * 1000, {})   # 同一天，50 分钟内提 6 次
    assert_(b["repeatStrength"] <= 1.01, f"同一天多次不应累加强度，实际 {b['repeatStrength']:.3f}")
    ra = float(a["repeatStrength"])
    assert_(ra > b["repeatStrength"] * 3, f"间隔重复的强度应远高于集中爆发：{ra:.2f} vs {b['repeatStrength']:.2f}")
    ha = half_life_of(a, None, t0 + 100 * DAY)
    hb = half_life_of(b, None, t0 + 100 * DAY)
    assert_(ha > hb, f"间隔重复的半衰期应更长：{ha:.1f} vs {hb:.1f}")
    print(f"PASS 9 间隔 > 爆发：间隔递增强度 {ra:.2f}（半衰期 {ha:.1f} 天）"
          f" vs 同日 6 次 {b['repeatStrength']:.2f}（半衰期 {hb:.1f} 天）")

    print("\n全部通过")


if __name__ == "__main__":
    main()
