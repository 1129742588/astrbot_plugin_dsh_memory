# 改动笔记 · astrbot 侧

对应 dsh 侧：`dsh/CHANGELOG.md`。原则是**协议与整理语义跟 dsh 一致**，只有接入方式不同。
每条都标了对应的差异编号（见审计报告 A/B/C 类），方便回溯。

## 2026-11-02 · 配置页：六组 + 自检

- `_conf_schema.json` 从「平铺 + `_head_*` 伪字段」改成 **6 组 object**（基础 / 注入 / 抓取 /
  整理 / 向量 / 高级）。`_head_*` 那种写法在配置页上会变成一个可填的字符串输入框，
  值还会被当成配置写回去；分组后标题是分组的 `description`，不占字段。
- `main.flatten_cfg()`：配置进门先摊平成一维，代码里照旧读 `cfg.get("quiet_min")`；
  老版本存的平铺配置仍然认（顶层键覆盖组内同名键，`_` 开头的旧伪字段直接丢掉）。
- 新暴露 4 项：`l2_keep_max`（10）、`summary_chunk`（60）、`embed_chunk`（32）、
  `warn_layer_items`（200000）。前三个原先写死在 organizer 里，用户改不了。
- `long_max_chars` 默认 4000 → **3000**，与 dsh 的 `memoryCfg.longMaxChars` 对齐
  （`main.DEFAULTS` / `organizer.ORG_DEFAULTS` / `tiers.Tiers` 三处一起改）。
- `test/test_schema.py`：键集合双向比对、类型、默认值、取值范围、分组齐全、`flatten_cfg`
  往返，26 项全查。配置页最容易出的毛病是「界面上有、代码里没有」（填了不起作用），
  这个测试就是防它。

## 2026-11-02 · 使用文档

- 新增 `README.md`：安装、六组配置逐项说明（附 dsh 对应项）、命令表、共用一份记忆、
  常见问题、目录结构。
- `metadata.yaml`：描述改成「自己抓取 + 自己整理」（原文还写着「整理交给 dsh」），
  补 `repo` / `short_desc`，版本对齐 0.8.2。

## 2026-11-02 · 目录结构：`core/` 是真源，根目录是副本

- 插件本体统一收在 `astrbot/core/`：`main.py`、`tiers.py`、`organizer.py`、`vectors.py`、
  `metadata.yaml`、`_conf_schema.json`。改代码只改这一份，两端结构对齐。
- 根目录保留同名副本：astrbot 从**插件目录根**读 `main.py` / `metadata.yaml` /
  `_conf_schema.json`，少了这三个市场里装不上、手动拷进 `data/plugins/` 也不认。
- `sync_core.py` 负责 `core/ → 根目录` 同步：`python sync_core.py` 拷贝，
  `python sync_core.py --check` 只比对（不一致退出码 1，可挂 CI）。
  根目录那份是副本，别手改——下次同步会被覆盖。
- `test/`、`ARCHITECTURE.md`、`CHANGELOG.md` 留在端根目录（不属于插件运行时）；
  测试里的 `CORE` 常量指回 `core/`。

## 2026-11-02 · 批次 1：与 dsh 对齐（语义 + 容量 + 日志）

### 修正

- **A1 摘要按 60 条分片**（`organizer.py`）
  `digest` 不再把整组塞给模型：超过 `summary_chunk`（默认 60）就切片，逐片调用，
  片内编号从 1 开始、合并时加回偏移，`maxLines` 按片长算。某片失败只损失那一片，
  它的原文走 DL1（不丢）。原先 10 万条会一次打出 190 万 tokens 的请求，直接 HTTP 400。
  与 dsh 的 `silicon.summarizeWithSources` 分片规则一致。

- **A2 嵌入按 32 条分批**（`organizer._embed_items`）
  整组一次传给 provider 改成 `embed_chunk=32` 分块、`embed_concurrency=4` 有界并发。
  失败粒度从"整组"降到"32 条"，失败批次记 `MODEL_FAIL embed chunk=N`。

- **A3 没有模型时下沉 DL1**（`organizer.digest` / `organize`）
  不再整轮跳过、也不再整组搬 L2：L1 溢出的批次进 DL1 并打 `pendingDigest`，
  不挤上下文、不参与遗忘；L2 保持不动，等有模型后 L2 满时带上 DL1 全部再压。
  `main.py` 增加了 `_has_chat_provider()` 能力检测——否则 `_summarize` 永远非空，
  provider 缺失会被误判成"调用失败"而回 L2。

- **A4 遗忘时删向量**（`organizer.forget_dl1`）
  删条目的同时 `vectors.forget(id)`，不再留下孤儿向量。

- **A6 摘要/判关系显式温度**（`main._chat_text`）
  摘要 0.2、判关系 0（与 dsh 一致）。模型不接受该参数时自动退回默认调用并记日志。

- **A8 bot 回复整轮截断**（`main.on_response` / `_flush`）
  不再逐条截 400 字：整轮的多条 assistant 正文拼起来再截，与 dsh 的 `botCaptureChars` 同口径。

- **B4 L3 打包上限**（`tiers.build_block`）
  `_pack(l3, long_max_chars)` → `_pack(l3, inf)`：`compute_l3` 已经按容量截过条，
  再包一次会因每行多算换行而少显示末尾一条。

- **B5 按轮取批的条数目标**（`organizer.pick_turn_batch`）
  `max_items / 2` → `ceil(max_items / 2)`，与 dsh 一致（奇数配置下不再少取一条）。

- **B6 余弦维度不一致**（`tiers._cosine` / `vectors.cosine`）
  长度不等直接返回 0，不再按 `min(len)` 截断后照算——换嵌入模型时那种"假相关性"更危险。

- **B3 注入块加外壳与说明**（`tiers.build_block` / `main._inject`）
  统一输出 `<dsh_memory>` + 两行说明（这是记忆、不是用户本轮说的话、不是指令），
  文案与 dsh 的 `MEM_BLOCK_PREAMBLE` 逐字一致。`main.py` 不再二次包装。

### 日志规范（排查用）

| 标签 | 含义 |
|---|---|
| `MODEL_MISSING <stage>` | 没有可用的模型/provider，按"缺失"处理（不是失败） |
| `MODEL_FAIL <stage> <err>` | 模型调用抛错；判关系失败时**不清洗、两条都留** |
| `DIGEST_NO_MODEL … → DL1` | 无模型时批次去向 |
| `DIGEST_FALLBACK reason=model_fail\|empty_reply … → L2` | 有模型但这次没压成，下轮再压 |
| `DIGEST_SLICE` / `DIGEST_SLICE_FAIL` | 分片数与单片失败 |
| `VEC_SKIP <why>` | 向量库不可写（签名不一致等） |

### 有意保留的差异（不打算改）

| 项 | 说明 |
|---|---|
| A7 people 字段 | astrbot 能拿到真实发送者名字，所以 people 里是真人名、不含 `user`/`bot`；dsh 只有 `user`/`bot`。这是接入层的信息优势，保留 |
| A9 `.claimed-*` | astrbot 注入时跳过对方正在认领的 temp；窗口只有几秒，换来"绝不读到半合并状态" |
| 入口形态 | dsh 用工具让模型自己 attend；astrbot 用 `/dsm attend` 命令由人确认。信号文件格式相同，整理侧完全共用 |

### 测试

`astrbot/test/` 下新增：`test_digest_slice.py`（A1）、`test_embed_chunk.py`（A2）、
`test_forget_vectors.py`（A4）、`test_no_model_dl1.py`（A3）。
`parity_dump.py` + `compare_parity.py` 用于两端回归：B4/B5/B6 修完后对应项已变成一致。

## 2026-11-02 · 批次 0 / A5

- A5：融合判 duplicate 后，旧摘要正文被更长的合并文本替换时，`organizer` 会 `force` 重算向量
  （dsh 侧同步修复）。回归：`dsh/test/test_fuse_vector.mjs`。
- 新增两端对照脚本，把"疑似差异"变成可复现证据；A7/A9/B4/B5/B6 均已实测确认。

## 2026-11-02 · 批次 2/3：性能（缓存 + 批量搬层 + 打分）

### 批次 2：只读层缓存与 id 索引

- `tiers._read_cached`：按 `(mtime_ns, size)` 缓存整层，对方原子替换后自动失效。
  单层超过 `layer_cache_max_items`（默认 30 万）不缓存，避免吃满内存。
- `read_layer_safe` / `read_l1_view` 走缓存；`organizer.load_db` 仍直读——
  缓存返回的是共享 list，整理会就地改对象，绝不能改到缓存。
- `tiers._index_of`：层内 `id → item` 索引，懒建、跟缓存条目一起失效；
  `search` 的 sources 下探改成按需查索引，不再每次重建一个大 dict。
- 实测 10 万/层：`build_block` 0.67s → **0.10s**（缓存命中），
  detail 检索 0.94s → **0.56s**（首次）；5 万条 L2 首读 133ms → 命中 0.2ms。

### 批次 3：批量搬层 / 超时 / 打分

- `organizer._move_many`：digest 路由先分桶（DL2 / L2 / DL1）再一次性搬，
  不再逐条扫 5 层。语义与 `_move` 完全一致（同层幂等、L2 记 `consolidatedAt`）。
  实测 10 万条搬层 **36ms**（原来 35 分钟）。`apply_signals` 的 forget 也改成一批只扫一遍层。
- `digest` 超时：每片前 `_check_timeout`、路由每 2000 条一次；
  `max_organize_ms=50` 时 172ms 退出，模型只调 1 次，锁已释放。
- 检索打分：关键词命中改逐字段短路（先查 text，没命中的才拼 people/scene）、
  只对通过过滤的建 dict、排序改 `heapq.nsmallest`（同分按原序）。
  `test_search_equiv.py` 用朴素实现对照 10 个查询，`(id, score)` 逐条一致。

### 没做 / 已知

- L2 每轮仍要排序（10 万条约 80ms）。要再压需要缓存"排好序的 L2"，收益不大，先留着。
- dsh 侧的打分未改（它 10 万条 91ms，够用）；公式与输出与 astrbot 仍逐条一致。

## 2026-11-02 · 批次 4：入口与检索补全

- **`/dsm attend / pin / unpin / forget`**：写 `signals/<writer>.jsonl`，字段与 dsh 的
  `signal()` 完全一致（`opId/op/ids/at[/v]`），由下一次整理消费。
  之前 astrbot 侧没有任何信号入口 —— 意味着 `recallCount` 永远是 0、
  DL1 不会因"关注"升回 L2、DL3 的热度也不会涨。逻辑写对了但永远走不到。
  `/dsm search` 的输出加了 id，方便直接复制进这些命令。
- **`semantic_min_sim` 生效**：`search` 的 hits 增加 `sem`（纯余弦），语义注入按它过滤。
  原先这个配置项读了但没用，而且就算用也不该拿混合分 `score` 当余弦比。
- **精排**：探测 `rerank_provider_insts`，有就接进 `search`，没有就不精排（不假装）。
  `advice` 的阈值按"有没有精排"分开：有 rerank 用 rel 的 0.5/0.1，
  没有则用综合分的 0.55/0.38 —— 否则建议文案永远是同一句。
- 测试：`test_signals_roundtrip.py`（attend 升层 / opId 防重放 / pin 翻转 / forget 写台账）；
  `test_search_equiv.py` 增加 `sem` 字段与 rerank 断言。

## 2026-11-02 · 可选项：L2 排序缓存 + 层容量告警

- **L2 排序缓存**（`tiers._sorted_l2`）：L2 每轮都要按 `consolidatedAt` 排一次，
  10 万条约 80ms。现在排序结果按层缓存签名记住，文件没变就直接复用。
  实测 `build_block` 缓存命中 **0.104s → 0.017s**。
- **层容量告警**（`organizer._warn_layers`）：整理时单层超过 `warn_layer_items`
  （默认 20 万）写一条 `LAYER_WARN L2=…`；`/dsm` 里超过 20 万的层会标 ⚠️，
  即时层（含 temp）超过 50 万条再提示一次。只告警，不改行为。
- `/dsm` 增加「层缓存：命中/未命中」，方便判断缓存是否在工作；`stats()` 增加
  `layers`（各层条数）与 `cache` 字段。
- 测试：`test_layer_warn.py`（超阈值写、未超不写）；`test_cache.py` 增加
  排序缓存与「L2 变了要重排」的断言。

## 2026-11-02 · 真机回放（74 条真实数据 + 硅基流动）发现的问题

- **垃圾回复被当成「零产出整理」**：模型回一段「抱歉，我无法完成」时，解析出的是
  一堆 `sources` 为空的摘要；原逻辑不触发 fallback，原文被静默路由进 DL1
  （不丢，但不会重压）。现在解析后若「没有任何带 sources 的摘要、也没有 keep」
  就视为失败，整批回 L2。dsh 侧同步改。
- **精排池太小**：只把 `limit*2`（默认 10）条送精排，dsh 是 `rerankPool=30`。
  相关条目经常在粗排就掉出池子，实测 bge-reranker 给相关文档 0.998 分，
  但那条根本没被送进去，`reranked` 永远是 False。改为
  `max(limit*2, rerank_pool)`，默认池 30。
- **rerank 返回字段**：硅基流动 `/rerank` 给的是 `relevance_score`，不是 `score`；
  直接取 `score` 会全变 0 分。现在两种字段都认。
- 真机结果与摘要质量见 `astrbot/test/live/REPORT.md`。

## 2026-11-02 · 时间锚点 / 通道分类 / 场景修正

### 1. 摘要里的时间：相对时间全部丢失（真问题）

**现象**：原文「今天中午…下周一（2026-11-02）还有开题报告」→ 摘要「星鲸拒绝代写论文，
说可以陪理思路但字得自己敲」；「明天要出去打工」→ 摘要里连"打工"都没了。
实测 38 条摘要里 25 条的原文含「今天/明天/下周」，摘要里几乎全部消失。

**根因不是时间戳丢了**，是两层：
- 条目的 `createdAt` / `spanFrom` / `spanTo` 一直都在，注入渲染 `- 10-30 [dsh·user]` 也没问题；
- 但**送模型的行只有 `说话人：正文`，没有日期**。模型看不到这条是哪天发生的，
  「明天」就没有锚点，只能丢掉或原样照抄（照抄更糟——过几天再读没人知道是哪天）。

**改法**：
- `organizer.raw_line()` / `memory.rawLine()`：送模型的行变成 `[MM-DD] 说话人：正文`
  （与注入块的日期口径一致，都是本地时区）
- 两端 prompt 加一条：「原文行首的 [月-日] 是这一条发生的时间。原文里的
  「今天/明天/昨天/下周/上周三」要按那个日期折算成具体日期再写进摘要；
  原文里已有的日期照抄。摘要里不要出现「今天」这种离开原文就看不懂的词。」
- `write_turn` 支持调用方传 `createdAt`（补录历史/导入旧库必需）。
  原来只能用写入时刻——回放真实数据时所有条目都记成"现在"，
  日期前缀全是当天，相对时间根本没法折算。

**效果**（同一份 74 条真实数据）：
| | 改前 | 改后 |
|---|---|---|
| 摘要含具体日期/星期 | 12/46（几乎都是原文自带的） | **21/31** |
| 摘要残留「今天/明天」 | 1 条 | **0 条** |
| 例 | 「星鲸拒绝代写论文…」 | 「…呕木屋买买买2026-11-02有开题报告」<br>「10-30 下午2点开万圣节秘境」<br>「答应2026-10-31少说点省着花」 |

### 2. 通道分类：两端都没真正跑起来

**现象**：真实回放里 `channel` 100% 是 `dialog`、`importance` 100% 是 `0.5`。
于是 `_FOCUS` 的 action/event 分支永远走不到，检索打分的 imp 分量恒定，
DL1 的遗忘曲线对所有条目一视同仁。

**根因**：
- astrbot 侧根本没有分类链（`main.py` 写死 `channel="dialog"`，不传 importance）
- dsh 侧有 `refineImportance`，但调用点写在 `routeMode !== 'citation'` 的分支里，
  而默认就是 citation → **那条链从来没执行过**（死代码）

**改法（两端一致）**：
- 写入路径**不调模型**，只打 `impRough` 标记（写入多一次往返就是一两秒延迟，
  而且刚写进来的条目可能马上被合并/作废，那时分类纯属浪费）
- 整理时、digest 分组**之前**整批分类（20 条一次请求）——channel 参与分组，
  分晚了本轮还是按 dialog 合批，白分
- 只走模型，不用本地正则假装分过；模型失败/解析失败**保留 impRough**，下轮再试，
  记 `MODEL_FAIL classify` / `CLASSIFY 0/N`
- 批量解析严格按 `i` 对齐；漏项就是 None，**不能**按位置拿下一项顶上
  （那会把两件事的记忆互相污染）
- dsh 的 `classifyBatch` 去掉本地预筛（原来「今天有点困」这类不问模型直接给 0.25，
  导致同一批里一部分模型判、一部分正则判，出问题分不清是谁判错的）
- 新增 `createMemory` 作用域的 `refineImportance`：合并文件后 split 主线原本没有这条链，
  只有 `createLegacyMemory` 那份，两者作用域不共享

**效果**（同一份数据）：`dialog 57 / action 7 / event 10`（改前 dialog 100%）。

### 3. 场景（scene）是测试脚本取错了字段

`livingmemory` 的 `session_id` 是 `群名:消息类型:群号`（如 `大肥鱼:GroupMessage:966657516`），
`dataset.py` 写成 `split(":")[1:2]` 取到的是**消息类型**，于是整个回放库的 scene
变成 `qq·GroupMessage` / `qq·FriendMessage`——等于把"哪个群"的信息丢了。

改成取第 0 段（群名），`FriendMessage` 归为 `qq·私聊`。修后：
`qq·大肥鱼 57 / qq·项目群 12 / qq·私聊 4 / qq·硬件群 1`。

**插件本身没问题**：`main._scene()` 一直是 `{平台}·{群名}` 或 `{平台}·私聊`，
platform 取自 `event.get_platform_name()`。抖音/B站接入后 scene 自动变成
`bilibili·xxx`，分组与检索过滤天然隔离，不需要改代码。

### 4. 成本变化

同一份 74 条：chat 调用 17 → 28 次（分类 4~5 次 + channel 分组变多导致摘要分批），
prompt tokens 49k → 61k。换来的是 channel/importance 真实、摘要带日期。

## 2026-11-02 · 注入位置 / 历史污染 / 计数衰减

### 1. 注入位置改成「系统提示词末尾」（与 dsh 一致）

原来默认 `extra_user_part`——挂在本轮用户消息**之后**。你指出的顺序是
`系统提示 + 工具/技能 + 人格 + L3/L2/L1 + 当前对话`，改法：

- `inject_target` 默认值改为 `system_prompt`：`req.system_prompt += 记忆块`。
  `on_llm_request` 拿到的 system_prompt 已经含工具与人格，追加即落在它们之后。
- dsh 一直是 `systemPrompt.context({ order: 250 })`，也在系统提示词尾部 —— 两端一致。

### 2. 记忆块被写进会话历史（真 bug）

`extra_user_content_parts` 里的 `TextPart` **默认会持久化到会话历史**，
只有 `.mark_as_temp()` 才只对本轮生效。原代码没调，后果：

- 每轮往历史里塞一份 4000 字记忆，十轮后上下文被记忆灌满
- 下一轮模型同时看到「上轮的旧记忆」和「本轮的新记忆」，冲突时无从判断
- 历史里那段每轮都变，等于把前缀缓存整段作废

现在三个分支都标了临时：`TextPart(text).mark_as_temp()`、
dict 形式带 `_no_save: True`、contexts 分支同样带 `_no_save`。

### 3. 融合次数 / 关注次数：从裸计数器改成会衰减的强度

原来 `fusionCount` / `recallCount` 只增不减，**也不参与任何计算**——
DL3 进上下文的排序只看 `importance × heat`。结果是「一年前被重复 20 次」
永远压过「昨天刚被关注 3 次」。

现在：
- 原始计数保留（台账可审计），参与计算的是衰减后的有效值
- `effective_count = count × 0.5^(t/半衰期)`，等价于艾宾浩斯 `R = e^(-t/S)`
  （S = 半衰期/ln2）；半衰期由 `repeatStrength` 决定，被重复得越多忘得越慢
- 融合与关注各记 `lastFusedAt` / `lastRecalledAt`，两个指标互不干扰
- `l3_score = importance × heat × (1 + 0.25·log2(1+融合) + 0.35·log2(1+关注))`

实测：60 天前融合 10 次 → 有效 0.408（低于今天融合 2 次的 2.0）；
90 天前关注 8 次 → 0.066（低于今天关注 1 次的 1.0）。
回归：`test_strength_decay.py`（7 项断言，含半衰期、间隔效应、老数据兼容）。

### 4. 提示词对照加进回归

新增 `astrbot/test/compare_prompts.py`：从 dsh 源码提取 prompt 文本，
与 astrbot 的常量逐行 diff。顺手抓出一处真实差异——
dsh 的 `关键结果、"以后一律…"` 用的是英文引号，astrbot 是「」，已统一为中文引号。

## 2026-11-02 · 历史截断 / 强度回落 / 上下文查看

### 1. `context_keep`：截掉 astrbot 自带的对话历史

记忆系统已经存了原话（L1），历史再留一份是重复。新增配置：

- `0`（默认）全清，只留开头的 system 消息
- `-1` 不动（astrbot 原样）
- `N>0` 只留最近 N 轮

注意取舍：keep=0 时前缀 = system+记忆+本轮消息，缓存最稳；
keep=N 时窗口每轮滑动、"最近 N 轮"的第一个元素在变，前缀反而不稳。
**要么全清、要么不动，中间档省的是 token 不是缓存。**
全清后模型只能靠记忆块了解之前发生过什么，连续追问可能接不上。

### 2. 记忆强度会回落（艾宾浩斯的"间隔重复"，补上后半句）

原来只做了前半句：`spacingQuality` 让"隔得久"加分多、同一天多次不累加。
但 `repeatStrength` 只增不减——**久不提，强度也不会掉**，半衰期永远不变。

现在：
- `effective_strength`：距上次提及每过 `heat_half_life × heat_idle_factor`（默认 10×3=30 天）
  折半，下限回到 1
- `half_life_of` 用回落后的强度 → 久不提 → 半衰期变短 → **衰减变快**（这是你说的"衰减率也会增大"）
- `touch_heat` 里先把旧强度按曲线回落、再加本次的间隔质量，否则回落永远被抹掉

实测（`test_strength_decay.py` 第 8/9 项）：
- 强度 4.72 → 刚提过 4.7 / 30 天 2.9 / 120 天 1.2，半衰期 **17.5 → 13.5 天**
- 按 1/6/19/40/100 天间隔提 6 次 = 强度 4.72（半衰期 17.5 天）
  vs 同一天连提 6 次 = 强度 1.00（半衰期 13.0 天）

### 3. `/dsm ctx`：看当前注入的上下文

- `/dsm ctx` —— 总览：整块字数、每段条数/字数/首条示例、即时层隐藏了几条
- `/dsm ctx l3｜l2｜l1｜sem` —— 看某一段全文（限 3000 字，截断会提示）
- `/dsm ctx all` —— 整块（限 3000 字）
- `/dsm ctx file` —— 导出全文到 `<dir>/ctx_dump.txt`（内容长时看文件比刷聊天窗口方便）

底层是新增的 `Tiers.inspect_block()`：把 `build_block()` 的结果按 `## ` 切段，
不重复一遍拼装逻辑（否则两处会漂移）。

## 2026-11-02 · 外壳去掉端标识 / 命令帮助

### 1. `<dsh_memory>` → `<memory_context>`

外壳原来叫 `<dsh_memory>`——这是两端共享的核心逻辑，却带了某一端的名字。
换个宿主就得改一遍，而且会让模型以为自己在跟某个特定系统对话。

两端一起改成 `<memory_context>`（`tiers.MARK_OPEN` / `memory.MEM_BLOCK_OPEN`）。
剩下的 `dsh_memory` 只有两处，都不是给模型的文本：
插件数据目录名 `astrbot_plugin_dsh_memory`（astrbot 的插件目录约定，改了会丢数据）
和日志前缀 `[dsh_memory]`（方便在日志里定位是哪个插件）。

### 2. `/dsm help`

之前只有状态，没有命令清单。现在 `/dsm help`（或 `/dsm 帮助`、`/dsm ?`）列出全部子命令：
状态 / ctx（总览·分段·all·file）/ search / org / attend / pin / unpin / forget。

## 2026-11-02 · 命令名与 dsh 对齐

`/dsm` 这个名字是「dsh memory」的缩写，属于端特有标识。改成 `/memory`，
与 dsh 的工具名 `memory_*` 对齐；`/dsm` 保留为兼容别名（两个命令指向同一实现 `_mem_cmd`）。

同时补齐 dsh 有、astrbot 缺的子命令：

- `/memory write 内容` —— 手动记一条，进即时层，整理时补 channel/importance（对应 dsh 的 `memory_write`）
- 其余一一对应：`search`↔`memory_search`、`attend`↔`memory_attend`、
  `forget`↔`memory_forget`、无参数状态↔`memory_stats`

`pin` / `unpin` / `ctx` / `org` 是 astrbot 特有的（dsh 靠工具和设置页），保留。
