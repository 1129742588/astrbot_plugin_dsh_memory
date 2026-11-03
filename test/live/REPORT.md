# 真实数据 · 真实模型回放报告

时间：2026-11-02 21:03–21:18
模型：`deepseek-ai/DeepSeek-V4.1-Flash`（chat）+ `BAAI/bge-m3`（embed）+ `BAAI/bge-reranker-v2-m3`（rerank），全部走硅基流动
数据：astrbot 里 livingmemory 插件的真实库（`documents` 60 条）+ 14 条构造数据，共 **74 条 / 20,701 字**

构造数据专门覆盖机制边界：同义重复、前后矛盾、同一件事的进展、800 字长文、60 天前的流水账、多场景（群聊/私聊/另一个群）。

两端各自独立库、同一份数据、同一套模型。

---

## 一、结果速览

| | astrbot | dsh |
|---|---|---|
| 步骤通过 | **15 / 15** | 11 / 12（唯一未过是「精排」项的数据问题，机制本身另测通过） |
| 首次整理 | merged=74，L1→=43，DL1=39，DL2=4，DL3=5 | merged=74，L1→=43，DL1=40，DL2=3，DL3=3 |
| 二次整理 | l2Moved=31，DL3 14→22 | l2Moved=40，DL3 3→14 |
| 向量 | 82 条 | 57 条 |
| 注入块 | 9,257 字 / 78 行 | 7,417 字 / 53 行 |
| 耗时 | 43s（首跑）/ 10.5s（复用库） | 47s |
| API | chat 4 · embed 10 · rerank 3 · 约 2.3 万 prompt tokens | chat 4 · embed 10 · rerank 3 |

两端的「首次整理」数字几乎一致（`merged=74`、`l1Moved=43` 完全相同），说明分轮取批与路由逻辑同步；`DL1/DL2/DL3` 的差异来自模型每次输出几条摘要（temperature 0.2 仍有随机性），不是逻辑差异。

---

## 二、机制逐项验证

| 机制 | 证据 |
|---|---|
| 写入 → 认领 → 合并 | 74 条 temp 全部 `merged=74` |
| L1 溢出分流 | `L1→=43`，DL1=39/DL2=4 分流符合「被引用 > `+` > 其余」 |
| 摘要入库 | DL3 有摘要，全部带 sources |
| L2 二次整理 | 强制 L2 超容量后 `l2Moved=31`，DL3 从 14 增到 22 |
| 融合 | 判关系调用发生（日志有 FUSE 相关），重复/进展条目被合并或共存 |
| DL1 遗忘 | 独立库里 2 条 90 天前的被忘、刚写的保留（`forgotten=2, left=1`） |
| 向量 | 68~82 条补齐，`sig=api:BAAI/bge-m3` 可写 |
| 语义检索 | 3 个查询各命中 8~11 条（含 sources 下探） |
| 精排 | 自查询 `reranked=True`，rel = 0.998 / 0.995 / 0.743 |
| 信号 | attend 后 `recallCount+1` 且升到 L2；pin 生效；forget 条目消失 |
| 注入块 | `<dsh_memory>` + 说明两行 + 三段（L2 空时只有两段，符合预期） |

---

## 三、突发情况

| 场景 | 表现 | 结论 |
|---|---|---|
| **模型 401（key 失效 / 没钱）** | `MODEL_FAIL summarize HTTP 401` → `DIGEST_FALLBACK reason=model_fail` → 整批回 L2 | ✅ 不丢、留痕、下轮可重压 |
| **完全没有模型** | L1 溢出批次 → DL1 + `pendingDigest`，L2 保持 0 | ✅ 不挤上下文、不丢、免遗忘 |
| **模型返回垃圾（“抱歉，我无法完成”）** | 修复前：解析成无 sources 的摘要 → 原文静默进 DL1；修复后：整批回 L2 | ✅ 已修（见第五节） |
| **进程被 kill -9** | 锁文件残留 → 超过 `stale_lock_ms` 后新进程接管、放锁，原文 4 条一条不少 | ✅ 4/4 断言通过 |
| **别人持锁时抢整理** | 立即返回 `busy`，不硬等 | ✅ |
| **向量库签名不一致** | `writable=False` → 只读，不写 | ✅（单测覆盖） |
| **单片摘要失败** | 只损失该片，其余照常（`test_digest_slice`） | ✅ |

---

## 四、摘要质量（重点）

客观指标（不看文笔，只看可核算的部分）：

| 指标 | astrbot（34 条） | dsh（14 条） |
|---|---|---|
| 有 sources | 34 / 34 | 14 / 14 |
| 数字幻觉率 | **0** | **0** |
| 数字保留率 | 0.28 | 0.435 |
| 术语保留率 | 0.406 | 0.218 |
| 平均压缩比 | 0.117 | 0.113 |

样例（573 字原文 → 32 字摘要）：

> 星鲸拒绝代写论文，说可以陪理思路但字得自己敲，被查出来害的是本人

**好的方面**：没有编造（0 幻觉）、每条都能追到原文、句子可读、主体清楚。

**短板**：压缩比只有 0.11——573 字只留 32 字，数字保留率 0.28。意思是对了，但**条件、数量、时间这些细节基本被丢掉**。原因有两层：

1. prompt 里单条摘要限得很短（≤40 字），模型严格遵守；
2. 模型倾向「一条原文配一条摘要」，很少把同一件事的多条原文合并成一条（`sources` 几乎都是 1）。

**建议**（未改，等你定）：把单条上限放宽到 60~80 字、并在 prompt 里加一句「数字、人名、时间、决定的条件必须保留」；这会改变两端摘要风格，需要同时改 dsh 的 `summarizeWithSources` 并重跑对比。

---

## 五、这轮真跑出来的两个 bug（已修）

1. **垃圾回复被当成“零产出整理”**
   模型回一段“抱歉…”时，解析出一堆 `sources` 为空的摘要，不触发 fallback，原文被静默路由到 DL1（不丢，但不会重压）。
   修复：解析后若「没有任何带 sources 的摘要、也没有 keep」→ 视为失败，整批回 L2。两端同步。

2. **精排池太小**
   `astrbot` 只把 `limit*2=10` 条送精排，dsh 是 `rerankPool=30`。相关条目经常在粗排阶段就掉出池子，精排等于没生效（实测 bge-reranker 给相关文档 0.998，但那条根本没被送进去）。
   修复：精排池改为 `max(limit*2, rerank_pool=30)`，与 dsh 对齐。

另外修正了一处接口适配：硅基流动 `/rerank` 返回的字段是 `relevance_score` 而不是 `score`，原来直接取 `score` 会全变 0 分。

---

## 六、复现

```bash
# 1) 生成数据集（从 livingmemory.db 只读提取 + 构造补充）
python astrbot/test/live/dataset.py

# 2) 两端各跑一遍（key 走环境变量，不落盘）
SILICON_API_KEY=... python -u astrbot/test/live/run_astrbot.py
SILICON_API_KEY=... node  dsh/test/live/run_dsh.mjs

# 3) 复用已有库只跑后半段（检索/信号/异常），省掉重复整理
REUSE=1 SILICON_API_KEY=... python -u astrbot/test/live/run_astrbot.py

# 4) 摘要质量
python astrbot/test/live/eval_summaries.py
python astrbot/test/live/eval_summaries.py astrbot/test/live/run/dsh-lib

# 5) 精排机制（用库里已有摘要自查询）
SILICON_API_KEY=... node dsh/test/live/check_rerank.mjs

# 6) 强杀 / 抢锁
python astrbot/test/live/test_crash_lock.py
```

进度日志：`astrbot/test/live/run/progress.log`（每步一行，带时间戳）。
结构化结果：`run/astrbot-result.json`、`run/dsh-result.json`、`run/summary-quality.json`。

---

## 七、没覆盖到的

- 真正的模型限流（429）与超时重试：本次没触发，silicon 有退避逻辑但未实测。
- 两端同时整理同一份共享库：只用同端两个进程验证了锁互斥，跨端共享目录未实跑（协议相同）。
- 百万级数据：本次是 74 条，性能数字见 `perf_probe.py`（10 万/层）。
