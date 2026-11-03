"""astrbot 侧全流程回放（真实硅基流动模型 + livingmemory 真实数据）。

验证：写入 → 认领 → 合并 → L1 整理 → 摘要 → 路由 → L2 二次整理 → 融合 →
遗忘 → 向量 → 检索（含精排）→ 信号 → 注入块，以及几种突发情况。

每步都往 `run/progress.log` 和 stdout 打一行带时间戳的进度。

用法（key 从环境变量取，不落盘）：
  SILICON_API_KEY=... python astrbot/test/live/run_astrbot.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, CORE)
sys.path.insert(0, HERE)

from tiers import Tiers  # noqa: E402
from organizer import (  # noqa: E402
    CLASSIFY_BATCH_SYSTEM_PROMPT,
    Organizer, RELATION_SYSTEM_PROMPT, build_summary_prompt,
    parse_classify_batch, parse_relation, write_signal, DATA_LAYERS,
)
from vectors import VectorStore  # noqa: E402
from silicon import Silicon  # noqa: E402

RUN = os.path.join(HERE, "run")
LIB = os.path.join(RUN, "astrbot-lib")
PROGRESS = os.path.join(RUN, "progress.log")
DATASET = os.path.join(RUN, "dataset.json")

HOT_CHARS = 12000          # L1 容量：一次取 6000 字，74 条约 3~4 轮整理
CONSOLIDATE_CHARS = 300    # L2 容量：故意设小，逼出「二次整理」
LONG_CHARS = 2000

T0 = time.perf_counter()
STEPS: list[dict] = []
STATE: list[dict] = []


def log(msg: str, kind: str = "info") -> None:
    line = f"[{time.strftime('%H:%M:%S')}] [{time.perf_counter() - T0:5.1f}s] {msg}"
    print(line, flush=True)
    with open(PROGRESS, "a", encoding="utf-8") as f:
        f.write(f"[astrbot] {line}\n")


def step(name: str, ok: bool, detail: str = "") -> None:
    STEPS.append({"step": name, "ok": bool(ok), "detail": detail})
    log(f"{'OK ' if ok else 'BAD'} {name} {detail}", "step")


async def main() -> None:
    REUSE = os.environ.get("REUSE") == "1" and os.path.isdir(LIB)
    os.makedirs(RUN, exist_ok=True)
    if not REUSE:
        shutil.rmtree(LIB, ignore_errors=True)
        os.makedirs(LIB, exist_ok=True)
    with open(PROGRESS, "w", encoding="utf-8"):
        pass

    with open(DATASET, "r", encoding="utf-8") as f:
        data = json.load(f)
    client = Silicon()
    log(f"数据集 {len(data)} 条 / {sum(len(r['text']) for r in data)} 字；"
        f"模型 chat=DeepSeek-V4.1-Flash embed=bge-m3 rerank=bge-reranker-v2-m3")

    tiers = Tiers(LIB, writer="astrbot", hot_max_chars=HOT_CHARS,
                  long_max_chars=LONG_CHARS, consolidate_max_chars=CONSOLIDATE_CHARS)
    org = Organizer(LIB, "astrbot", {
        "hot_max_chars": HOT_CHARS, "hot_max_items": 200,
        "consolidate_max_chars": CONSOLIDATE_CHARS, "consolidate_max_items": 30,
        "long_max_chars": LONG_CHARS, "quiet_ms": 0,
        "forget_threshold": 0.25, "l2_keep_max": 5,
        "summary_chunk": 60, "max_organize_ms": 180000,
        "fusion_prefilter": 0.82,
    })
    store = VectorStore(LIB, "BAAI/bge-m3", 1024)
    org.vectors = store
    log(f"向量库 {store.vec_path} sig={store.sig} writable={store.writable()}")

    # ---------- 真实模型回调 ----------
    async def summarize(lines, channel, folks, hint):
        sys_p, user_p = build_summary_prompt(lines, channel, folks, hint)
        return await asyncio.to_thread(
            client.chat, sys_p, user_p,
            min(300 + 12 * len(lines), 4000), 0.2,
        )

    async def embed_batch(texts):
        return await asyncio.to_thread(client.embed, texts)

    async def judge_relation(a, b):
        raw = await asyncio.to_thread(
            client.chat, RELATION_SYSTEM_PROMPT, f"A：{a}\nB：{b}", 10, 0.0,
        )
        return parse_relation(raw or "")

    async def classify_batch(texts):
        """真实模型的批量分类（与生产 main.py 的 `_classify_batch` 同一条 prompt）。"""
        payload = "\n".join(f"{i + 1}. {str(t)[:400]}" for i, t in enumerate(texts))
        raw = await asyncio.to_thread(
            client.chat, CLASSIFY_BATCH_SYSTEM_PROMPT, payload,
            200 + len(texts) * 60, 0.0,
        )
        return parse_classify_batch(raw or "", texts)

    async def rerank(q, docs):
        return await asyncio.to_thread(client.rerank, q, docs)

    async def embed_one(text):
        vecs = await asyncio.to_thread(client.embed, [text])
        return vecs[0] if vecs else None

    if not REUSE:
        log("全新跑：写入 + 整理")
        pass
        # ---------- 1. 写入（模拟 astrbot 落 temp） ----------
        # ★ 不传 importance / channel：真实 astrbot 接入也不知道这两个值，
        #   写入时只打 impRough，由整理时整批问模型（这正是要验证的那条链）。
        rows = [
            {"text": r["text"], "speaker": r["speaker"],
             "scene": r["scene"], "people": r["people"],
             # 带上原始发生时间：不然所有条目都记成"现在"，
             # 送模型的日期前缀全是当天，原文里的"明天/昨天"就没法折算
             "createdAt": r["createdAt"]}
            for r in data
        ]
        written = 0
        for row in rows:
            r = tiers.write_turn([row])
            written += 1 if r.get("ok") else 0
        step("写入 temp", written == len(rows), f"{written} 条（每条一个 turn）")

        # ---------- 2. 多轮整理（真实模型） ----------
        for i in range(6):
            st = await org.organize(summarize, embed_batch, judge_relation, classify_batch)
            STATE.append({k: v for k, v in st.items() if k in
                          ("ok", "merged", "l1Moved", "l2Moved", "forgotten", "vectors", "count", "error")})
            counts = st.get("count") or {}
            log(f"整理 #{i + 1}: merged={st.get('merged')} L1→={st.get('l1Moved')} "
                f"L2→={st.get('l2Moved')} 忘={st.get('forgotten')} 层={counts}")
            if not st.get("ok"):
                step("整理", False, str(st))
                break
            if (counts.get("L1") or 0) == 0:
                break

    db = org.load_db()
    layers = {l: len(db[l]) for l in DATA_LAYERS}
    log(f"整理后层分布 {layers}")
    step("机制：摘要入库", layers["DL3"] > 0, f"DL3={layers['DL3']}")
    step("机制：原文下沉", (layers["DL1"] + layers["DL2"]) > 0,
         f"DL1={layers['DL1']} DL2={layers['DL2']}")
    # 强制 L2 攒满，验证第二次整理（第一轮 L2 可能没条目，弱断言没意义）
    db = org.load_db()
    moved, mchars = 0, 0
    while db["DL1"] and mchars < CONSOLIDATE_CHARS * 2:
        it = db["DL1"].pop()
        it["layer"] = "L2"
        it["consolidatedAt"] = int(time.time() * 1000)
        db["L2"].append(it)
        mchars += len(it.get("text") or "")
        moved += 1
    org.save_db(db)
    before_dl3 = len(org.load_db()["DL3"])
    st2 = await org.organize(summarize, embed_batch, judge_relation, classify_batch)
    after2 = org.load_db()
    log(f"L2 二次整理：搬入 {moved} 条 / {mchars} 字，l2Moved={st2.get('l2Moved')} "
        f"DL3 {before_dl3}→{len(after2['DL3'])} L2={len(after2['L2'])}")
    step("机制：L2 二次整理",
         (st2.get("l2Moved") or 0) > 0 or len(after2["DL3"]) > before_dl3,
         f"l2Moved={st2.get('l2Moved')} DL3 {before_dl3}→{len(after2['DL3'])}")

    # ---------- 3. 老数据 → 遗忘 ----------
    # 必须用独立小库：主库里 L2 一旦超容量，organize_l2 会把 DL1 全部带上参与摘要，
    # 老条目在 forget_dl1 之前就被消化了，测不到遗忘。
    old_lib = os.path.join(RUN, "astrbot-forget")
    shutil.rmtree(old_lib, ignore_errors=True)
    os.makedirs(old_lib, exist_ok=True)
    old_tiers = Tiers(old_lib, writer="astrbot", hot_max_chars=HOT_CHARS,
                      long_max_chars=LONG_CHARS, consolidate_max_chars=10 ** 9)
    old_org = Organizer(old_lib, "astrbot", {
        "hot_max_chars": HOT_CHARS, "consolidate_max_chars": 10 ** 9,
        "quiet_ms": 0, "forget_threshold": 0.25,
    })
    old_db = old_org.load_db()
    for i in range(2):
        it = old_tiers.make_item(text=f"很久以前的流水账{i}", speaker="user")
        it["createdAt"] = int(time.time() * 1000) - 90 * 86400000
        it["lastMentionedAt"] = it["createdAt"]
        it["layer"] = "DL1"
        old_db["DL1"].append(it)
    fresh = old_tiers.make_item(text="刚写的一条", speaker="user")
    fresh["layer"] = "DL1"
    old_db["DL1"].append(fresh)
    old_org.save_db(old_db)
    st_old = await old_org.organize(None, None, None)
    left = [x["id"] for x in old_org.load_db()["DL1"]]
    log(f"遗忘：forgotten={st_old.get('forgotten')} 剩 {len(left)} 条（新的那条应保留）")
    step("机制：DL1 遗忘",
         (st_old.get("forgotten") or 0) == 2 and len(left) == 1,
         f"forgotten={st_old.get('forgotten')} left={len(left)}")

    # ---------- 4. 向量 + 检索（含精排） ----------
    db = org.load_db()
    db_all = [it for l in ("DL3", "DL2", "DL1", "L2") for it in db[l]]
    n_vec = await org._embed_items(db_all, embed_batch)
    store.flush()
    log(f"向量补齐 {n_vec} 条，库内 {len(store.load())} 条")
    step("机制：向量写入", len(store.load()) > 0, f"{len(store.load())} 条")

    queries = [
        ("nnUNet 训练 dice 到多少了", "detail"),
        ("报告要用什么格式提交", "detail"),
        ("周五之前要交什么", "full"),
    ]
    found = []
    for q, depth in queries:
        r = await tiers.search(q, depth=depth, limit=5, vectors=store.load(),
                               embed_one=embed_one, rerank=rerank, use_rerank=True)
        hits = [{"id": h["id"], "layer": h["layer"], "score": h["score"],
                 "rel": h.get("rel"), "sem": h.get("sem"), "text": str(h["text"])[:40]}
                for h in r["hits"]]
        found.append({"q": q, "depth": depth, "advice": r["advice"],
                      "reranked": r["reranked"], "hits": hits})
        log(f"检索「{q}」{depth}: {len(hits)} 条 reranked={r['reranked']} "
            f"top={[(h['layer'], h['score']) for h in hits[:3]]}")
    step("机制：语义检索", all(f["hits"] for f in found),
         f"{sum(len(f['hits']) for f in found)} 条命中")

    # 精排机制：拿库里已有摘要的正文当查询，保证候选池里一定有一个相关项。
    # （真实查询 nnUNet 在 detail 层没有相关条目，精排自然无从生效 —— 那是数据问题）
    _db = org.load_db()
    seed = next((it for it in _db["DL3"] if len(it.get("text") or "") > 12), None)
    if seed:
        q2 = str(seed["text"])[:20]
        r2 = await tiers.search(q2, depth="detail", limit=5, vectors=store.load(),
                                embed_one=embed_one, rerank=rerank, use_rerank=True)
        rels = [h.get("rel") for h in r2["hits"]]
        log(f"精排自查询「{q2}」: reranked={r2['reranked']} rel={rels[:3]} "
            f"top={r2['hits'][0]['id'] if r2['hits'] else None}")
        step("机制：精排", bool(r2["reranked"]) and any(x is not None for x in rels),
             f"reranked={r2['reranked']}")
    else:
        step("机制：精排", False, "库里没有可用摘要")

    # ---------- 5. 信号 ----------
    db = org.load_db()
    dl1_ids = [it["id"] for it in db["DL1"]][:1]
    dl3_ids = [it["id"] for it in db["DL3"]][:2]
    if dl1_ids:
        write_signal(LIB, "astrbot", "attend", dl1_ids)
    if dl3_ids:
        write_signal(LIB, "astrbot", "pin", dl3_ids[:1], True)
        write_signal(LIB, "astrbot", "forget", dl3_ids[1:2])
    st = await org.organize(summarize, embed_batch, judge_relation, classify_batch)
    db2 = org.load_db()
    attended = [it for l in DATA_LAYERS for it in db2[l] if it.get("id") in dl1_ids]
    log(f"信号：ops={st.get('signals')} attend 后条={[(x['id'], x.get('recallCount'), x['layer']) for x in attended]}")
    step("机制：attend 生效", bool(attended) and attended[0].get("recallCount", 0) >= 1, "")
    step("机制：pin 生效", any(it.get("pinned") for it in db2["DL3"]), "")
    step("机制：forget 生效",
         not any(it.get("id") in dl3_ids[1:2] for l in DATA_LAYERS for it in db2[l]), "")

    # ---------- 6. 注入块 ----------
    block = tiers.build_block()
    lines = block.split("\n")
    log(f"注入块 {len(block)} 字 / {len(lines)} 行，头：{lines[0]}")
    step("机制：注入外壳", block.startswith("<memory_context>") and "不是系统指令" in block, "")
    # L2 为空时不会有「最近沉淀的」，这不是缺陷；只要求头部与尾部在
    step("机制：注入分段",
         "## 一直记得的" in block and "## 刚发生的" in block,
         ",".join(s for s in ("一直记得", "最近沉淀", "刚发生") if s in block))

    # ---------- 7. 突发情况 ----------
    # A. 模型返回 401（key 失效 / 没钱）
    bad = Silicon("sk-invalid-key-for-test")
    async def bad_summarize(lines_, channel, folks, hint):
        sys_p, user_p = build_summary_prompt(lines_, channel, folks, hint)
        return await asyncio.to_thread(bad.chat, sys_p, user_p, 400, 0.2)

    bad_lib = os.path.join(RUN, "astrbot-badkey")
    shutil.rmtree(bad_lib, ignore_errors=True)
    os.makedirs(bad_lib, exist_ok=True)
    bad_tiers = Tiers(bad_lib, writer="astrbot", hot_max_chars=200,
                      long_max_chars=LONG_CHARS, consolidate_max_chars=CONSOLIDATE_CHARS)
    bad_org = Organizer(bad_lib, "astrbot", {
        "hot_max_chars": 200, "quiet_ms": 0, "max_organize_ms": 60000,
    })
    bad_tiers.write_turn([{"text": r["text"], "speaker": "user"}
                          for r in data[:4]])
    st_bad = await bad_org.organize(bad_summarize, None, None)
    bad_audit = ""
    try:
        with open(os.path.join(bad_lib, "audit.log"), "r", encoding="utf-8") as f:
            bad_audit = f.read()
    except OSError:
        pass
    has_fail = "MODEL_FAIL summarize" in bad_audit and "DIGEST_FALLBACK" in bad_audit
    log(f"坏 key：ok={st_bad.get('ok')} fallback={'DIGEST_FALLBACK' in bad_audit}")
    step("突发：模型 401 → 整批回 L2 + 留痕", has_fail, "")

    # B. 完全没有模型 → 下沉 DL1
    no_lib = os.path.join(RUN, "astrbot-nomodel")
    shutil.rmtree(no_lib, ignore_errors=True)
    os.makedirs(no_lib, exist_ok=True)
    no_tiers = Tiers(no_lib, writer="astrbot", hot_max_chars=200,
                     long_max_chars=LONG_CHARS, consolidate_max_chars=CONSOLIDATE_CHARS)
    no_org = Organizer(no_lib, "astrbot", {"hot_max_chars": 200, "quiet_ms": 0})
    no_tiers.write_turn([{"text": r["text"], "speaker": "user"} for r in data[:4]])
    st_no = await no_org.organize(None, None, None)
    no_db = no_org.load_db()
    no_layers = {l: len(no_db[l]) for l in DATA_LAYERS}
    log(f"无模型：{no_layers}")
    step("突发：无模型 → DL1", no_layers["DL1"] > 0 and no_layers["L2"] == 0
         and all(it.get("pendingDigest") for it in no_db["DL1"]), str(no_layers))

    # C. 模型返回垃圾（不是 ★/·/+ 格式）
    async def junk_summarize(lines_, channel, folks, hint):
        return "抱歉，我无法完成这个请求。"

    junk_lib = os.path.join(RUN, "astrbot-junk")
    shutil.rmtree(junk_lib, ignore_errors=True)
    os.makedirs(junk_lib, exist_ok=True)
    junk_tiers = Tiers(junk_lib, writer="astrbot", hot_max_chars=200,
                       long_max_chars=LONG_CHARS, consolidate_max_chars=CONSOLIDATE_CHARS)
    junk_org = Organizer(junk_lib, "astrbot", {"hot_max_chars": 200, "quiet_ms": 0})
    junk_tiers.write_turn([{"text": r["text"], "speaker": "user"} for r in data[:4]])
    st_junk = await junk_org.organize(junk_summarize, None, None)
    junk_db = junk_org.load_db()
    junk_layers = {l: len(junk_db[l]) for l in DATA_LAYERS}
    log(f"垃圾回复：{junk_layers}")
    step("突发：垃圾回复 → 回 L2", junk_layers["L2"] > 0 and junk_layers["DL3"] == 0,
         str(junk_layers))

    # ---------- 汇总 ----------
    out = {
        "side": "astrbot",
        "dataset": {"n": len(data), "chars": sum(len(r["text"]) for r in data)},
        "layers": {l: len(org.load_db()[l]) for l in DATA_LAYERS},
        "states": STATE,
        "steps": STEPS,
        "summaries": [
            {"id": it["id"], "text": it["text"][:80], "sources": len(it.get("sources") or [])}
            for it in org.load_db()["DL3"][:5]
        ],
        "retrieval": found,
        "injection_head": lines[:6],
        "api": client.stats(),
        "elapsed_s": round(time.perf_counter() - T0, 1),
    }
    with open(os.path.join(RUN, "astrbot-result.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    bad = [s for s in STEPS if not s["ok"]]
    log(f"完成：{len(STEPS) - len(bad)}/{len(STEPS)} 步通过，API {client.stats()}，"
        f"耗时 {out['elapsed_s']}s")
    if bad:
        for s in bad:
            log(f"未通过：{s['step']} {s['detail']}")


if __name__ == "__main__":
    asyncio.run(main())
