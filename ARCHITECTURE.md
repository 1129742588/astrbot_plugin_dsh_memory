# 核心逻辑（两端共用）

> 本文件前半是**两端逐字相同**的核心逻辑，后半是各端适配。
> 改核心逻辑必须同时改两份；改适配只改自己那份。
> 回归：`astrbot/test/compare_prompts.py`（提示词）、`compare_parity.py`（纯函数输出）、
> `test_strength_decay.*`（衰减公式）——任何一端改歪都会以退出码 1 报错。

## 定位

两个插件是同一套记忆系统的两个宿主适配：**核心逻辑一模一样，只有接入方式不同**。
它们可以各自独立跑，也可以共用同一个数据目录（各写各的 temp，整理靠文件锁互斥）。

命令/工具名统一用 `memory` 前缀，注入外壳用中性标签，不出现任何一端的名字——
换个宿主只需要写适配层，核心不用动。

## 五层数据

同一种条目结构，靠 `layer` 分流：

| 层 | 放什么 | 进上下文 | 会遗忘 |
|---|---|---|---|
| L1 即时层 | 刚发生的原话（含所有端的 temp） | 进（按容量取头部） | 溢出即下沉 |
| L2 缓冲层 | 观察过一轮的原话 | 进 | 到期再压 |
| DL1 流水账 | 没被摘要引用的原文 | 不进 | 会（按保持率） |
| DL2 原文库 | 被摘要引用过的原文 | 不进 | 不会 |
| DL3 长久层 | 摘要 | 按分数截取进上下文 | 不会（只进不出，超容量才挤） |

条目字段（两端同名，只是 astrbot 用 snake_case、dsh 用 camelCase）：

```
id / text / kind(raw|summary) / channel(dialog|action|event|note) / layer
speaker(角色占位) / people(真人名) / scene(平台·群名|平台·私聊)
importance(0.85|0.6|0.25) / heat / repeatStrength / lastMentionedAt
fusionCount / lastFusedAt / recallCount / lastRecalledAt
supersededBy / createdAt / spanFrom / spanTo / sources / srcKey
pinned / impRough / turnId / origin
```

`origin` 是**写入端标识**（哪个程序写的），不是内容类型——内容类型是 `channel`。

## 一次整理（九步）

```
organize()
  ├─ acquireLock      新文件独占创建才算拿到；心跳；被接管就停手
  ├─ reconcile        同一 id 只能在一层；DL2 不留孤儿（孤儿降 DL1）
  ├─ mergeTemps       认领所有端的 *.jsonl（rename 成功才算收下）→ 按 id 去重进 L1
  ├─ applySignals     消费 attend / pin / forget（opId 防重放）
  ├─ organizeL1       L1 超容量 → 按轮取"安静了 quiet 分钟"的旧一半
  │                   → refineImportance（分类）→ digest（摘要+路由）
  ├─ organizeL2       L2 超容量 → 取旧一半 + DL1 全部 → 第二次摘要
  ├─ forgetDL1        保持率低于阈值 → 先写台账再删（写不进去就不删）
  ├─ saveDb           先深层后 L1（崩了也不丢原文）；DL3 没变就跳过重写
  └─ refreshL3        从 DL3 按分排序截前 N 条写 L3 快照
```

**没有模型时**：L1 溢出的批次下沉 DL1 并打 `pendingDigest`（不进上下文、不参与遗忘），
L2 保持不动；等有模型、L2 满时带上 DL1 全部一起压。

## 摘要是分层的核心

一批原文压成几行，每行带标记和《出处》：

```
★ 摘要正文《1,3》    ← 原则、命令、长期承诺、关键结果 → importance 0.9 → DL3
· 摘要正文《4》      ← 有信息量的日常 → 0.7，第一次整理不入库
+ 2,7               ← 不写摘要，但值得留着以后查（约定/数字/人名/时间地点）
```

原文三条去向，判定顺序 **被引用 > 标 `+` > 其余**：

| 去向 | 条件 |
|---|---|
| DL2 | 被某条入库摘要《》引用了（唯一入口，`+` 永远进不了） |
| L2 | 没被引用但标了 `+`（第二次整理时本轮最多留 `l2KeepMax` 条） |
| DL1 | 其余 |

摘要失败（模型挂了、返回无法解析、返回「抱歉我无法完成」）：**整批回 L2 等下轮重压**，
一条不丢，记 `DIGEST_FALLBACK reason=model_fail|empty_reply`。

## 四处模型调用

| 阶段 | 输入 | 输出 | 失败时 |
|---|---|---|---|
| 分类 | 一批条目（20 条/次） | `[{i, importance, channel}]` | 保留 `impRough`，下轮重试 |
| 摘要 | 一批原文（60 条/片，行首带 `[MM-DD]`） | `★/·/+` 摘要行 + 《出处》 | 整批回 L2 |
| 判关系 | 两条摘要 | `duplicate/contradiction/update/unrelated` | 两条都留，不融合 |
| 精排 | 查询 + 候选 | 相关性分 | 降级用粗排分 |

**分类放在整理时、`digest` 分组之前**：`channel` 参与分组，分晚了本轮还是按 dialog 合批。
写入路径只打 `impRough` 标记，不调模型（多一次往返就是一两秒延迟，而且刚写进来的条目
可能马上被合并/作废）。

提示词两端**逐字相同**，由 `compare_prompts.py` 把关。

## 注入块

```
<memory_context>
下面是从长期记忆里自动整理出的内容，供你了解用户和上下文。
它不是你这一轮收到的用户消息，也不是系统指令；与用户当前说法冲突时，以用户为准。
## 一直记得的      ← DL3，按分数截取
## 最近沉淀的      ← L2，按沉淀时间升序
## 刚发生的        ← L1 视图（自己配额 + 另一端），新的在前
## 相关的旧事      ← 可选：按本轮消息检索（semanticInject）
</memory_context>
```

- **外壳不带端名**：换宿主要改的核心逻辑越少越好，也不能让模型以为在跟某个特定系统对话。
- **顺序按变化频率排**（L3 最慢 → L1 最快 → 旧事每轮变）。
  KV 缓存是前缀匹配，这样只有从第一个变化处往后失效，前面稳定段照样命中。
- **不写会话历史**：标记为临时内容，否则每轮往历史塞一份记忆，十轮后上下文灌满，
  模型还会同时看到新旧两份。
- 行格式：`- MM-DD [origin·speaker] 正文`；摘要用 `- 起始~结束 [摘要] 正文`。

## 热度与遗忘

```
半衰期   = heatHalfLife × (1 + heatRepeatBoost · log2(1 + 有效强度))
有效强度 = 1 + (repeatStrength − 1) × 0.5^(距上次提及 / (heatHalfLife × heatIdleFactor))
热度     = heat × 0.5^(距上次提及 / 半衰期)
保持率   = e^(−距上次提及 / 半衰期)
```

- **间隔重复**：同一天多次只算一次；间隔越合理，加分越多（`spacingQuality`）
- **久不提，连强度都会退**：`有效强度` 每 `heatIdleFactor` 个半衰期折半，下限 1
  → 半衰期变短 → **衰减更快**（这是"衰减率也会增大"）
- **融合/关注分别衰减**：`fusionCount` / `recallCount` 各记 `lastFusedAt` / `lastRecalledAt`，
  有效值同样按遗忘曲线折半
- **DL3 排序分**：`importance × 热度 × (1 + 0.25·log2(1+有效融合) + 0.35·log2(1+有效关注))`
  —— 很久没被重复/关注的摘要，加成自动归零，不会靠历史次数永远霸占上下文名额

## 协议细节（踩过的坑）

- 只追加，写完立即关闭（Windows 上被打开的文件没法被另一端 rename 认领）
- UTF-8 无 BOM、行尾 `\n`、`ensure_ascii=False`
- `createdAt` 必须是**毫秒**；`id` 必须带随机（两个进程的计数器都从 1 开始）
- 一轮一个 `turnId`；同一轮的第二次 LLM 调用（工具循环）复用同一轮，不重复记用户
- 读到半行 JSON 只跳过那一行，不整份放弃
- 不读、不删 `L1.tmp/*.claimed-*`（那是对方正在合并的）
- 向量库按模型签名分文件；签名对不上只读不写（写了会让对方判定换模型、丢掉整个库）
- 补录历史时必须传 `createdAt`，否则全记成"现在"，送模型的日期前缀全错

## 三条不能碰的线

1. **不写入判重**。重复度是重要性信号，提前合并会抹掉它。
   （历史上曾有「写入时判重」，60 天烧掉 844 次调用 / 16.6 万 token / 58%，已废除。）
2. **原文时间戳不得变**。`createdAt` 一旦写入不再改，否则遗忘曲线算不准。
3. **搜到不涨热度**。只认 `attend` 确认，否则误命中的记忆越搜越热，成回音室。

## 设计理由（为什么这么定）

这些都踩过坑，不是拍脑袋定的。改之前先看理由，否则容易把坑重新踩一遍。

**为什么 L3（DL3）只进不出**
长期记忆不该被日常冲刷。淘汰是**容量驱动**不是热度驱动：没满时哪怕热度降到 0 也留着，
只有超字数上限才按热度升序挤；而且不挤"刚够格进来的"（热度 ≥ 晋升线的不参与淘汰），
否则一条刚晋升的会立刻被挤出，来回抖。

**为什么摘要条数是 `ceil(n/10)`**
早期是 `n/5`（40 条出 8 条全进 DL3），等于没压缩——长期层被日常琐事灌满，真正的原则被稀释。
改成 `n/10` 才符合"大部分内容只当原文沉下去"。上限是**刹车不是指标**：
模型会为凑数把不相干的事缝成一行（实测「周末爬山 + 地铁故障 + 鸡蛋涨价」被压成一句），
所以 prompt 同时写"最多 M 条"和"少写完全可以"。

**为什么分类放在整理时、而不是写入时**
① 写入路径每多一次模型往返就多一两秒延迟；② 刚写进来的条目可能马上被合并/作废/遗忘，
那时分类纯属浪费；③ 整批 20 条一次请求，成本约 1/20。
但必须在 `digest` 分组**之前**——`channel` 参与分组，分晚了本轮还是按 dialog 合批。

**为什么注入块要带「这是记忆」的说明**
这段文字和用户消息一起进上下文。不说明来源，模型会把它当成"用户刚说的话"或"系统指令"，
甚至照着里面的旧约定去执行。外壳标签用中性名（不带端名），换宿主不用改核心。

**为什么写入时不判重**
重复度是重要性信号（同一件事被反复说 = 重要），提前合并会抹掉它。
历史上做过"写入时判重"，60 天烧掉 844 次调用 / 16.6 万 token / 58% 成本，已废除。

**为什么原文时间戳不能改**
遗忘曲线全部基于 `createdAt` / `lastMentionedAt`。时间戳一动，保持率、半衰期、衰减全错。

**为什么搜到不涨热度**
只认 `attend`（明确确认相关）。否则检索的误命中会让记忆越搜越热，形成回音室。

**为什么 DL2 只放「被摘要引用过」的原文**
DL2 的语义是"这条原文曾被某条摘要引用，要查细节来这里"。
放开入口（比如"重要就进 DL2"）会让它变成第二个 L2，失去"可追溯"的意义。

**为什么没有模型时下沉 DL1 而不是搬 L2**
DL1 不进上下文、打 `pendingDigest` 后也不参与遗忘——既不挤上下文也不丢。
搬 L2 会占上下文位置，而且 L2 满了会被误压成假摘要。

**为什么摘要要分片（60 条/片）、嵌入要分批（32 条/批）**
整组一次塞给模型，10 万条时 HTTP 400；整组一次嵌入，失败就整组丢向量。
分片/分批后失败粒度降到"这一片/这一批"，其余照常。

---
---

# astrbot 端适配

## 目录

```
astrbot/
├── core/                    ← 源码真源，改代码只改这里
│   ├── main.py              插件层
│   ├── tiers.py             协议层
│   ├── organizer.py         整理层
│   ├── vectors.py           向量层
│   ├── metadata.yaml        插件元信息
│   └── _conf_schema.json    配置页 schema
├── <同名文件>                 ← 由 core/ 复制出来的副本，给安装器/市场用
├── sync_core.py             core → 根目录 同步（--check 只比对）
├── ARCHITECTURE.md / CHANGELOG.md
└── test/                    测试（不属于插件运行时，CORE 指回 core/）

```

根目录那份是副本：astrbot 从插件目录根加载 `main.py`、读 `metadata.yaml` 与
`_conf_schema.json`，所以根上必须有。别手改副本——`python sync_core.py` 会覆盖它。

## 文件

| 文件 | 作用 |
|---|---|
| `tiers.py` | 协议层：写 temp、读各层、拼注入块（纯标准库，可脱离 astrbot 单独跑） |
| `organizer.py` | 整理层：锁、认领、合并、按轮取批、摘要分流、遗忘、L3 快照 |
| `vectors.py` | 向量层：与 dsh 同格式的向量库、签名保护、余弦检索 |
| `main.py` | 插件层：astrbot 钩子、注入位置、模型调用、命令、定时整理 |

## 接入点

| 环节 | 本端做法 |
|---|---|
| 写入触发 | `on_llm_request` 起轮 → `on_llm_response` 收回复 → `after_message_sent` 落盘 |
| 注入位置 | `system_prompt` 末尾（默认）；可切 `extra_user_part` / `contexts_user` |
| 历史处理 | `context_keep` 可截断 astrbot 自带历史（记忆系统已存原话，避免重复） |
| 模型 | astrbot 的 chat / embedding / rerank provider |
| 入口 | `/memory` 命令（`/dsm` 兼容别名） |
| 历史压缩 | **不做**——astrbot 有自己的会话管理，不属于记忆系统 |

## 数据目录

默认各玩各的：`dir` 留空 → `<astrbot data>/plugin_data/astrbot_plugin_dsh_memory/memory`，
从零初始化一份独立记忆库。

想共享就把两边指到同一个目录，两端必须各自用不同的 `writer`（`dsh` / `astrbot`），
否则会写同一个 temp 抢行。共享时两边进程要能看见同一个路径（docker 要挂载宿主目录）。

## 配置页

`_conf_schema.json` 分六组（astrbot 的 `type: object` + `items`，配置页按组折叠），
代码里由 `flatten_cfg()` 摊平成一维再读——这样"界面分组"和"代码取值"互不影响。

| 组 | 键 | 默认 | 说明 |
|---|---|---|---|
| 基础 | `dir` | 空 | 数据目录；空 = 用插件自己的 |
| | `writer` | `astrbot` | temp 文件名与来源标记，只能是 `[A-Za-z0-9_-]` |
| 注入 | `inject` | 开 | 关掉只写不读 |
| | `inject_target` | `system_prompt` | 另可选 `extra_user_part`（缓存更优）、`contexts_user` |
| | `context_keep` | 0 | 对话历史截断：`-1` 不动 / `0` 全清 / `N` 留最近 N 轮 |
| | `hot_max_chars` | 3000 | L1 注入容量 |
| | `hot_quota_own` | 0.7 | 自己先占 70%，剩下给另一端 |
| | `long_max_chars` | 3000 | L3 注入容量 |
| | `consolidate_max_chars` | 2500 | L2 注入容量 |
| 抓取 | `capture` / `skip_commands` | 开 / 开 | 总开关 / `/` 开头的消息不记 |
| | `capture_user_chars` / `capture_bot_chars` | 2000 / 400 | 截断（bot 按整轮拼后截） |
| 整理 | `organize` / `organize_every_min` | 开 / 5 | 自己整理 + 定时补漏 |
| | `quiet_min` / `forget_threshold` | 10 / 0.25 | 只整理安静下来的轮次 / 遗忘阈值 |
| | `l2_keep_max` / `summary_chunk` | 10 / 60 | 每轮最多沉淀几条 / 摘要分片 |
| 向量 | `embed_model` / `embed_chunk` | 空 / 32 | 嵌入模型（空 = 用 provider）/ 分批 |
| | `semantic_inject` / `semantic_top_k` / `semantic_min_sim` | 关 / 5 / 0.35 | 「相关旧事」 |
| 高级 | `warn_layer_items` / `debug` | 200000 / 关 | 层容量告警 / 调试日志 |

改完 `test/test_schema.py` 会跑一遍：键集合、类型、默认值、范围、分组、`flatten_cfg` 摊平，
任一对不上就报错——避免"界面上有、代码里没有"这种填了不起作用的坑。

## 命令（`/memory`，`/dsm` 等价）

```
/memory                    状态（各层条数、向量库、整理情况）
/memory write 内容          手动记一条
/memory search 关键词       跨五层检索（结果带 id）
/memory attend <id...>     关注：热度+1，DL1 的会回 L2
/memory forget <id...>     忘掉（先写台账）
/memory pin / unpin <id...> 钉住 / 取消
/memory ctx                当前注入的上下文（分段统计）
/memory ctx l3|l2|l1|sem   某一段全文
/memory ctx all            整块（截断 3000 字）
/memory ctx file           导出整块到文件
/memory org                立刻整理一次
```

命令在 QQ 等平台要"被 @ 或唤醒"才触发（astrbot 的过滤规则），命令本身不进记忆。

## 本端特有

- `/memory pin|unpin|ctx|org`：dsh 靠工具参数和设置页，astrbot 用命令更方便
- `_scene()`：`平台·群名` / `平台·私聊`（抖音、B站接入后自动变成 `bilibili·xxx`）
- `_sender()`：拿真实昵称写进 `people`（dsh 宿主暂时拿不到）
- 无模型时整轮跳过摘要，只把溢出批次下沉 DL1

---
---

# 实现细节

## 与 dsh 的逐项对齐

核心逻辑两边一模一样，只有接入方式不同。逐项对照（改任何一项都要两端一起改）：

| 能力 | dsh | 本端 |
|---|---|---|
| 写当前轮 | `writeTurn` → `L1.tmp/<writer>.jsonl` | 同（`write_turn`） |
| 读 L1 视图 | `readL1View`（L1 + 所有 temp，按 id 去重） | 同（`read_l1_view`） |
| 整理主流程 | `organize()` 九步 | 同（顺序一致） |
| 抢锁 / 心跳 / 接管 | `acquireLock` / `heartbeat` / `checkLock` | 同 |
| 认领 temp | rename 成功才算收下 | 同 |
| 对账 | 同 id 只在一层；DL2 孤儿降 DL1 | 同 |
| 按轮取批 | 整轮、不切断问答、pinned 不动 | 同（`pick_turn_batch`） |
| 分类 | `refineImportance`（20 条/批） | 同（`refine_importance`） |
| 摘要 + 出处 | `summarizeWithSources`（★/·/+ 与《编号》） | 同（prompt 逐字一致） |
| 摘要融合 | 向量预筛 → 判关系 → duplicate/contradiction/update | 同 |
| 热度 / 衰减 | `touchHeat` / `heatOf` / `spacingQuality` / `effectiveStrength` | 同（数值逐位一致） |
| 遗忘 | `forgetDL1`（保持率 < 阈值，先写台账） | 同 |
| 信号 | attend / pin / forget + opId 防重放 | 同 |
| 写盘顺序 | 先深层后 L1；DL3 没变就跳过 | 同 |
| L3 快照 | 版本号 + 节流 | 同 |
| 检索 | 关键词 + 向量 + 时间窗 + 分层 + 精排 + 顺 sources 下探 | 同 |
| 无模型时 | L1 溢出批次 → DL1 + `pendingDigest` | 同 |
| 摘要失败 | 整批回 L2 等下轮重压 | 同 |
| 注入外壳 | `<memory_context>` + 「这是记忆」说明 | 同（逐字一致） |
| 向量库 | `deep-part-0000.json` + `deep-meta.json`（sig 保护） | 同 |
| 「相关旧事」 | `semanticInject`（异步预取 + TTL） | 同（本端能当轮用上，dsh 可能延后一轮） |

## 向量库：三条安全线

和 dsh 共用同一个向量库，格式一字不差：

```
{"<id>": "<base64 of Float32Array>"}           deep-part-0000.json
{"sig":"api:BAAI/bge-m3","dim":1024,"count":N} deep-meta.json
```

1. **签名必须一致**。库里的 `sig` 与当前模型名对不上 → 只读不写。
   硬写会让 dsh 判定“换了模型”，把整个向量库丢掉（它的 `VEC_STALE` 分支）。
2. **没有模型名就不写**。宁可不建库，也不能写出空 `sig` 污染对方的 meta。
3. **只有持锁的一方写向量**。整理时在数据定稿后批量补，补不上下次还有机会。

换嵌入模型必须两端一起换，否则余弦是在比两个不同的空间。

## 安装与部署

```bash
插件目录：<astrbot data>/plugins/astrbot_plugin_dsh_memory/
docker compose up -d astrbot      # 挂载变了必须重建容器，restart 不生效
```

没有 `requirements.txt`——纯标准库。

## 阶段

A 写 temp + 读 L1 注入 → B 读 L2/L3 三段注入 → C 自己拿锁整理 → D 向量 + 语义检索。

做完 C 就能**独立跑**：不装 dsh 也能写、整理、摘要、遗忘；dsh 同时开着就靠锁互斥。
D 只是在上面加了语义检索，不做也不影响独立运行。

## 为什么本文件比 dsh 的短

`dsh/core/ARCHITECTURE.md` 有 2600 多行，本文件只有 300 行左右，不是内容缺失：

- 两边**前 142 行核心逻辑逐字相同**（可直接 diff）
- dsh 那份多出来的是**设计史**：为什么 L3 只进不出、为什么摘要按 n/10 而不是 n/5、
  为什么 `pack(l3, Infinity)`、每次性能实测的数据、每个踩过的坑的复盘
- 本端是 dsh 逻辑的移植实现，那些推导过程不重复抄一遍——要看设计理由去 dsh 那份的
  「实现细节与历史决策」章节，那里是唯一权威
- 本端自己的实现细节（协议坑、向量安全线、逐项对照）在上面的章节里
