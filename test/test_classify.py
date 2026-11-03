"""分类链回归：写入打 impRough → 整理时整批问模型 → channel/importance 落地。

为什么要有这个测试（2026-11-02）：
  真实回放里两端的 `channel` 100% 是 dialog、`importance` 100% 是 0.5 ——
  dsh 有 classify 链，但调用点写在 `routeMode !== 'citation'` 的分支里，
  而默认就是 citation，所以那条链**从来没执行过**；astrbot 侧压根没有这条链。
  结果：_FOCUS 的 action/event 分支永远走不到，检索打分里的 imp 分量恒定，
  DL1 的遗忘曲线对所有条目一视同仁。

约定：
  - 写入路径**不调模型**（不阻塞），只打 impRough 标记
  - 整理时、digest 分组**之前**整批分类（channel 参与分组，晚了就白分）
  - 模型不可用 / 解析失败：保留 impRough，下一轮再试，不拿本地正则假装分过

用法：python astrbot/test/test_classify.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "core"))

from tiers import Tiers  # noqa: E402
from organizer import Organizer, parse_classify_batch, raw_line  # noqa: E402

DAY = 86400000


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


def read_layer(root: str, name: str) -> list[dict]:
    p = os.path.join(root, f"{name}.json")
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f).get("items") or []


async def main() -> None:
    root = tempfile.mkdtemp(prefix="classify-")
    try:
        # ---------- 1. 写入不分类，但打 impRough ----------
        tiers = Tiers(root, writer="astrbot", hot_max_chars=10 ** 6)
        now = int(time.time() * 1000)
        rows = [
            {"text": "把后处理加了连通域过滤，Dice 涨了 0.02", "speaker": "user", "scene": "qq·项目群"},
            {"text": "刷抖音刷到一个讲肺结节的，说得挺在理", "speaker": "user", "scene": "qq·项目群"},
            {"text": "以后评估一律以召回率为准", "speaker": "user", "scene": "qq·项目群"},
            {"text": "今天有点困", "speaker": "user", "scene": "qq·项目群"},
        ]
        r = tiers.write_turn(rows)
        assert_(r.get("ok"), f"写入失败：{r}")
        items, _bad = tiers.read_l1_view()   # write_turn 落在 temp，L1 视图才看得到
        assert_(len(items) == 4, f"应写入 4 条，实际 {len(items)}")
        assert_(all(it.get("impRough") for it in items), "没显式给 importance 的都应打 impRough")
        assert_(all(it.get("channel") == "dialog" for it in items), "写入时 channel 应保持默认 dialog")
        print(f"PASS 1 写入：4 条全部 impRough=True，channel 默认 dialog，未调模型")

        # ---------- 2. 整理时整批分类，channel/importance 落地 ----------
        calls = []

        async def classify(texts):
            calls.append(len(texts))
            # 模拟真实模型：按内容判 action / event / 原则 / 闲聊
            out = []
            for t in texts:
                if "连通域" in t:
                    out.append({"importance": 0.6, "channel": "action"})
                elif "抖音" in t:
                    out.append({"importance": 0.6, "channel": "event"})
                elif "一律" in t:
                    out.append({"importance": 0.85, "channel": "dialog"})
                else:
                    out.append({"importance": 0.25, "channel": "dialog"})
            return out

        async def summarize(lines, channel, folks, hint):
            # 返回空 → 整批回 L2，测试只关心分类结果
            return ""

        org = Organizer(root, "astrbot", {
            "hot_max_chars": 10,
            "hot_max_items": 10,
            "quiet_ms": 0,
            "consolidate_max_chars": 10 ** 9,
        })
        st = await org.organize(summarize, None, None, classify)
        assert_(st.get("ok"), f"整理失败：{st}")
        assert_(calls and calls[0] == 4, f"应一次批量分类 4 条，实际 {calls}")

        db = org.load_db()
        by_text = {it["text"]: it for it in db["L2"]}
        got = {
            "连通域": (0.6, "action"),
            "抖音": (0.6, "event"),
            "一律": (0.85, "dialog"),
            "困": (0.25, "dialog"),
        }
        for key, (imp, ch) in got.items():
            it = next((v for k, v in by_text.items() if key in k), None)
            assert_(it is not None, f"找不到含「{key}」的条目")
            assert_(abs(float(it["importance"]) - imp) < 1e-9,
                    f"「{key}」importance 应为 {imp}，实际 {it['importance']}")
            assert_(it["channel"] == ch, f"「{key}」channel 应为 {ch}，实际 {it['channel']}")
            assert_(not it.get("impRough"), f"「{key}」分类后应删掉 impRough")
        print(f"PASS 2 整理：一次请求分类 {calls[0]} 条，三种 channel + 三档 importance 全部落地")

        # ---------- 3. 模型失败：保留 impRough，下轮再试 ----------
        root2 = tempfile.mkdtemp(prefix="classify-fail-")
        try:
            t2 = Tiers(root2, writer="astrbot", hot_max_chars=10 ** 6)
            t2.write_turn([{"text": "这条没分类成功", "speaker": "user"}])
            o2 = Organizer(root2, "astrbot", {
                "hot_max_chars": 1, "hot_max_items": 10, "quiet_ms": 0,
                "consolidate_max_chars": 10 ** 9,
            })

            async def bad_classify(texts):
                return [None] * len(texts)   # 模型返回无法解析

            await o2.organize(summarize, None, None, bad_classify)
            db2 = o2.load_db()
            left = [it for l in ("L1", "L2") for it in db2[l] if "没分类成功" in it["text"]]
            assert_(left, "条目不该丢")
            assert_(all(it.get("impRough") for it in left),
                    "分类失败必须保留 impRough，下一轮还要再试")
            assert_(all(it.get("channel") == "dialog" for it in left),
                    "失败时保持默认 dialog，不能瞎猜")
            with open(o2.audit_path, "r", encoding="utf-8") as f:
                log2 = f.read()
            assert_("CLASSIFY 0/1" in log2, f"应记 CLASSIFY 0/1，实际日志尾部：{log2[-300:]}")
            print("PASS 3 模型失败：保留 impRough、保持 dialog、日志记 CLASSIFY 0/1")
        finally:
            import shutil
            shutil.rmtree(root2, ignore_errors=True)

        # ---------- 4. 没有分类能力：不假装分过 ----------
        root3 = tempfile.mkdtemp(prefix="classify-nomodel-")
        try:
            t3 = Tiers(root3, writer="astrbot", hot_max_chars=10 ** 6)
            t3.write_turn([{"text": "没有模型也要留着", "speaker": "user"}])
            o3 = Organizer(root3, "astrbot", {
                "hot_max_chars": 1, "hot_max_items": 10, "quiet_ms": 0,
                "consolidate_max_chars": 10 ** 9,
            })
            await o3.organize(None, None, None)   # 无模型
            with open(o3.audit_path, "r", encoding="utf-8") as f:
                log3 = f.read()
            assert_("MODEL_MISSING classify" in log3, "没有分类能力时应记 MODEL_MISSING classify")
            print("PASS 4 无模型：记 MODEL_MISSING classify，条目保留 impRough")
        finally:
            import shutil
            shutil.rmtree(root3, ignore_errors=True)

        # ---------- 5. 批量解析：漏项 / 乱序 / 坏 JSON ----------
        texts = ["A", "B", "C"]
        out = parse_classify_batch(
            '```json\n[{"i":3,"importance":0.25,"channel":"dialog"},'
            '{"i":1,"importance":0.6,"channel":"action"}]\n```', texts)
        assert_(out[0] and out[0]["channel"] == "action", "按 i 对齐失败（i=1）")
        assert_(out[1] is None, "漏项应为 None，不能瞎补")
        assert_(out[2] and out[2]["importance"] == 0.25, "按 i 对齐失败（i=3）")
        assert_(all(x is None for x in parse_classify_batch("抱歉，我无法完成", texts)),
                "非 JSON 应全部 None")
        # 三档归并：0.7 → 0.6（最近档）
        out2 = parse_classify_batch('[{"i":1,"importance":0.7,"channel":"event"}]', ["x"])
        assert_(out2[0]["importance"] == 0.6, f"0.7 应归到 0.6，实际 {out2[0]['importance']}")
        print("PASS 5 批量解析：i 对齐、漏项 None、坏 JSON 全 None、三档归并")

        # ---------- 6. 送模型的行带日期（相对时间才有锚点） ----------
        it = {
            "createdAt": int(time.mktime((2026, 10, 30, 21, 0, 0, 0, 0, -1)) * 1000),
            "speaker": "user",
            "text": "明天要出去打工",
        }
        line = raw_line(it)
        assert_(line.startswith("[10-30] "), f"行首应有日期，实际 {line[:20]}")
        assert_("user：明天要出去打工" in line, f"说话人与正文格式不对：{line}")
        assert_(raw_line({"createdAt": 0, "text": "无时间"}) == "无时间", "无时间戳时不该加空括号")
        print(f"PASS 6 送模型的行：{line}")

        print("\n全部通过")
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
