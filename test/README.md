# 两端对照测试（只读）

目的：用**同一份 fixture** 跑 astrbot 与 dsh 的纯函数，把"疑似逻辑不一致"变成可复现的证据。
不修改 `core/`，不读写真实记忆库——fixture 全落在系统临时目录，脚本退出即删。

## 怎么跑

```bash
# 1) 各跑一遍，结果落到各自的 out/
python astrbot/test/parity_dump.py     # → astrbot/test/out/astrbot.json
node   dsh/test/parity_dump.mjs        # → dsh/test/out/dsh.json

# 2) 逐字段比对（有未登记差异时退出码 = 1，可用于回归）
python astrbot/test/compare_parity.py
```

`compare_parity.py` 已内置"已登记差异"白名单；出现白名单之外的偏差会以退出码 1 报错。

## fixture 覆盖

| 内容 | 说明 |
|---|---|
| L1 / L2 / DL1 / DL2 / DL3 | 3 / 2 / 1 / 1 / 4 条，含 1 条 `supersededBy`、2 条 740 字长摘要 |
| `L1.tmp/parity.jsonl` | 自己写的临时条目 |
| `L1.tmp/other.jsonl` | 另一端的临时条目 |
| `L1.tmp/x.claimed-*.jsonl` | 已认领未落盘（考 A9） |
| `long_max_chars=1500` | 让 L3 打包的边界差异显形（考 B4） |
| `max_items=5`（6 条 1 字条目） | 让按轮取批的取整差异显形（考 B5） |

## 2026-11-02 实测结果

| 项 | astrbot | dsh | 对应差异 |
|---|---|---|---|
| `l1_view` 条数 | 5 | 6（多 `m5_p6`） | A9：astrbot 跳过 `.claimed-*` |
| `pick_ids` | k1..k4（4 条） | k1..k3（3 条） | B5：`max_items/2` vs `ceil(max_items/2)` |
| `cosine_unequal` | 1.0 | 0 | B6：`min(len)` 截断 vs 长度不等直接 0 |
| `make_item_people` | `[]` | `["user"]` | A7：是否把 speaker 并进 people |
| 注入块 | 少两行 | — | B4（L3 末尾少一条长摘要）+ A9（少一条已认领） |
| `search`（detail，无向量） | `m5_s0, m5_d2` | 完全相同 | 检索打分与 sources 下探**已同步** |

结论：检索路径逐条一致；差异集中在「写入字段构造」（A7）、「按轮取批的取整」（B5）、
「注入时的层打包与 temp 可见性」（A9/B4）。A1/A2/A4/A6/A8 需要模型与 astrbot 运行时，
不在本脚本覆盖范围内，按计划用独立的 `test_digest_slice.py` / `test_embed_chunk.py` 等验证。

## 其他回归脚本

| 脚本 | 覆盖 | 断言 |
|---|---|---|
| `dsh/test/test_fuse_vector.mjs` | A5：融合判 duplicate、正文被更长的合并文本替换后，旧摘要的向量必须重算 | 合并前 `[1,3]`（旧正文 3 字）→ 合并后必须是 `[1,8]`（新正文 8 字）。注掉修复行会明确 FAIL，可用于防回退 |
| `astrbot/test/test_no_model_dl1.py`<br>`dsh/test/test_no_model_dl1.mjs` | A3：没有模型时不产生假摘要，L1 溢出批次下沉 DL1 | L1 清零、L2 保持 0、DL1 收下 2 条且都带 `pendingDigest`；把这两条的时间改到 100 天前再整理一次，DL1 仍是 2 条（免于遗忘） |
| `astrbot/test/test_digest_slice.py` | A1：摘要按 `summary_chunk` 切片 | 10 条 / 片长 3 → 4 次调用 `[3,3,3,1]`，`sources` 偏移为 `1/4/7/10`；第 2 片抛异常时只损失该片 |
| `astrbot/test/test_embed_chunk.py` | A2：嵌入按 32 分批 + 有界并发 | 100 条 → 4 批 `[32,32,32,4]`；一批失败只丢 32 条，其余 68 条照常写入 |
| `astrbot/test/test_forget_vectors.py` | A4：遗忘 DL1 时同步删向量 | 被遗忘的 id 从向量库消失，未遗忘的不受影响 |
| `astrbot/test/test_cache.py` | 批次 2：只读层缓存 + 懒建 id 索引 | 5 万条 L2 首读 133ms → 命中 0.2ms；外部写盘后能读到新内容；超过上限不缓存；id 索引只建一次 |
| `astrbot/test/test_move_batch.py`<br>`dsh/test/test_move_many.mjs` | 批次 3：批量搬层 | 10 万条搬层 36ms / 45ms（< 1s）；同层幂等、L2 记 `consolidatedAt`、与逐条搬结果一致 |
| `astrbot/test/test_digest_timeout.py` | 批次 3：超时检查进 digest 内部 | `max_organize_ms=50` → 172ms 退出、模型只调 1 次、`organize.lock` 已释放 |
| `astrbot/test/test_search_equiv.py` | 批次 3：检索打分优化等价 | 10 个查询的 `(id, score)` 与朴素实现（拼 body + 全排序）逐条一致；8 条同分取 6 条时按原序 |
| `astrbot/test/perf_probe.py` | 性能探针（只输出数字） | 10 万/层：`build_block` 首次 0.67s / 命中 0.10s；detail 检索首次 0.94s / 命中 0.56s |
| `astrbot/test/test_signals_roundtrip.py` | 批次 4：信号入口闭环 | attend 后 `recallCount+1` 且 DL1→L2；同 `opId` 重放不生效；pin/unpin 翻转；forget 删条并写台账 |
| `astrbot/test/test_layer_warn.py` | 可选项：层容量告警 | 单层超 `warn_layer_items` 写 `LAYER_WARN`，未超不写 |
| `astrbot/test/test_strength_decay.py`<br>`dsh/test/test_strength_decay.mjs` | 融合次数 / 关注次数的衰减 | 60 天前融合 10 次 → 0.408（低于今天 2 次的 2.0）；90 天前关注 8 次 → 0.066；过一个半衰期正好减半；`repeatStrength` 高的衰减更慢；缺 `lastFusedAt` 时退回 `createdAt`；两指标互不干扰 |
| `dsh/test/test_semantic_extra.mjs` | 「相关旧事」段的拼装 | 位置在「刚发生的」之后、闭合标签之前；空段不注入且输出与不带 extra 逐字一致；外壳无 `dsh_memory` 残留 |
| `astrbot/test/compare_prompts.py` | 两端提示词逐行对照 | 分类单条 30 行、批量 32 行、判关系 11 行、摘要 user 固定文案 24 行；有偏差退出码 1 |
| `astrbot/test/parity_dump.py` + `compare_parity.py` | 两端纯函数输出对照 | `line_of` / `build_block` / `pick_turn_batch` / `cosine` / `search` 逐字段 diff |
| `astrbot/test/test_classify.py`<br>`dsh/test/test_classify.mjs` | 通道分类链：写入打 `impRough` → 整理时整批问模型 → channel/importance 落地 | 写入不调模型但全打标记；一次批量请求后 action/event/dialog 与三档 importance 全部正确；模型失败保留 `impRough` 并保持 dialog；无模型记 `MODEL_MISSING classify`；批量解析漏项留 None 不瞎补；`raw_line` 行首带 `[MM-DD]` |
