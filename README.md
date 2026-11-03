# 记忆分层 · astrbot 端

让 astrbot 记住你说过的话，忘掉无关的，聊再久也不多花钱。

和 dsh 的 `dsh-memory-tiers` 是同一套协议的两端实现：**分层、整理、路由、注入块的语义完全一致**，
只有接入方式不同（这边走 astrbot 的钩子和命令，那边走 dsh 的 systemPrompt 和工具）。
两端指向同一个数据目录，就是**一份共享记忆**——在 dsh 里说的话，astrbot 这边下一轮就想得起来。

- 只有 astrbot：也能用，自己抓取、自己整理、自己注入。
- 想两端共用：看下面「共用一份记忆」。

架构细节看 [`ARCHITECTURE.md`](ARCHITECTURE.md)，改动记录看 [`CHANGELOG.md`](CHANGELOG.md)。

## 安装

1. 把本目录（`astrbot/`，包括里面的 `core/`）整个放进 `data/plugins/`，**目录名保持
   `astrbot_plugin_dsh_memory`**（插件用这个名字定位自己的 `plugin_data` 目录）。
   根目录下那份 `main.py` / `tiers.py` / … 是 `core/` 的副本，安装器读的就是它，别删。
2. 重启 astrbot，在「插件管理」里确认插件已加载。
3. 打开该插件的「配置」页，按需填「基础 → 数据目录」。
4. 保存后重启（配置是构造时读的）。

不想动配置页也能跑：数据目录留空 = 用插件自己的 `data/plugin_data/astrbot_plugin_dsh_memory/memory`。

## 配置页

配置分六组，和 dsh 设置页的分组、默认值一一对应（下表「dsh 对应项」一列可以直接抄）。

### 基础

| 项 | 默认 | 说明 |
|---|---|---|
| 数据目录 `dir` | 空 | 空 = 插件自己的目录；填 dsh 的目录 = 共用一份记忆 |
| 写入端名字 `writer` | `astrbot` | 决定 `L1.tmp/<writer>.jsonl`。**必须和 dsh 不同**，否则写同一个 temp |

### 注入

| 项 | 默认 | 说明 |
|---|---|---|
| 注入记忆 `inject` | 开 | 关掉 = 只写不读，用来验证抓取 |
| 注入位置 `inject_target` | `system_prompt` | 也可 `extra_user_part`（前缀缓存最友好）/ `contexts_user` |
| 对话历史截断 `context_keep` | 0 | 0 = 全清（记忆里已有原话）；-1 = 不动；N = 留最近 N 轮 |
| 刚发生的容量 `hot_max_chars` | 3000 | L1 视图上限，`memoryCfg.hotMaxChars` |
| 自己那一端配额 `hot_quota_own` | 0.7 | L1 里先紧着自己写的占七成，余下留给另一端 |
| 一直记得的容量 `long_max_chars` | 3000 | L3 视图上限，`memoryCfg.longMaxChars` |
| 最近沉淀的容量 `consolidate_max_chars` | 2500 | L2 视图上限，`memoryCfg.consolidateMaxChars` |

### 抓取

| 项 | 默认 | 说明 |
|---|---|---|
| 记录每一轮 `capture` | 开 | 关掉后另一端看不到本端说的话 |
| 用户消息截断 `capture_user_chars` | 2000 | 0 = 不截断 |
| 机器人回复截断 `capture_bot_chars` | 400 | 对齐 dsh 的 `botCaptureChars` |
| 不记指令消息 `skip_commands` | 开 | `/memory`、`/reset` 之类不算对话 |

### 整理

| 项 | 默认 | 说明 |
|---|---|---|
| 自己整理 `organize` | 开 | 关掉 = 整理全交给 dsh；锁互斥，谁拿到谁整理 |
| 整理间隔 `organize_every_min` | 5 分钟 | 定时补漏 |
| 只整理安静多久的轮次 `quiet_min` | 10 分钟 | 避免聊到一半被摘要腰斩 |
| 遗忘阈值 `forget_threshold` | 0.25 | 流水账保持率低于此值就忘，忘前写台账 |
| 每轮最多沉淀几条 `l2_keep_max` | 10 | 其余落流水账（不丢，只是不常驻上下文） |
| 摘要分片 `summary_chunk` | 60 条 | 太大撑爆上下文，太小摘要碎片化 |

### 向量 / 语义检索

| 项 | 默认 | 说明 |
|---|---|---|
| 嵌入模型 `embed_model` | 空 | 空 = 用 astrbot 配的 embedding provider；必须与 dsh 同签名 |
| 嵌入分批 `embed_chunk` | 32 | 失败只丢一批 |
| 注入时带相关旧事 `semantic_inject` | 关 | 每轮多一次 embedding 调用 |
| 检索条数 `semantic_top_k` | 5 | |
| 相似度下限 `semantic_min_sim` | 0.35 | 调低有噪音，调高搜不到 |

### 高级

| 项 | 默认 | 说明 |
|---|---|---|
| 层容量告警 `warn_layer_items` | 200000 | 某层条数超了就在日志告警，防止文件无声涨到几个 G |
| 调试日志 `debug` | 关 | 每次注入 / 落盘打一行 |

## 命令

`/memory` 是主命令，`/dsm` 是等价别名（老写法，保留兼容）。命令名不带任何一端的标识。

| 命令 | 作用 |
|---|---|
| `/memory` | 看状态：各层条数、字数、向量库、整理情况 |
| `/memory help` | 列出全部子命令 |
| `/memory write 内容` | 手动记一条 |
| `/memory search 关键词` | 跨五层检索（结果带 id） |
| `/memory attend <id...>` | 关注：热度 +1，DL1 的会回 L2 |
| `/memory forget <id...>` | 忘掉：写台账后从库和向量里删 |
| `/memory pin / unpin <id...>` | 钉住 / 取消钉住（不参与遗忘和淘汰） |
| `/memory ctx` | 看当前注入块（各段条数、字数、一条示例） |
| `/memory ctx l3\|l2\|l1\|sem` | 看某一段全文 |
| `/memory ctx all` | 看整块（截断 3000 字） |
| `/memory ctx file` | 导出全文到文件 |
| `/memory peek` | 直接看注入块原文（截断 1500 字） |
| `/memory org` | 立刻整理一次 |

## 共用一份记忆

1. 把 astrbot 的「数据目录」填成 dsh 的数据目录（例如 `C:\Users\你\.dsh\storages\memory-v5`）。
2. 两端 `writer` 必须不同（`astrbot` / `dsh`）。
3. 两端进程要能看见同一个路径：docker 部署要把宿主目录挂进容器，配置里填容器内路径。
4. 嵌入模型必须一致（默认 `BAAI/bge-m3`），否则向量签名对不上——这时插件只读不写，不会把对方的库写废。

各玩各的（默认）也完全能用，只是两边记忆互不相通。

## 常见问题

**装完没反应？**
看日志里有没有 `[dsh_memory] 数据目录 … writer=astrbot`。没有 = 插件没加载；
有但注入块是空的 = 记忆库还没内容，先聊几轮，等一次整理。

**注入块一直是空的？**
检查 `inject` 是否被关掉、`dir` 是否指到了一个空目录；`/memory ctx` 会告诉你每段有几条。

**会不会让前缀缓存失效？**
`inject_target=system_prompt` 时记忆块每轮都变，它后面的历史无法命中缓存。想要最高命中率就改
`extra_user_part`；`context_keep=0`（默认）把 astrbot 自带历史全清掉，重复内容不会两头占。

**改了配置没生效？**
配置在插件构造时读入，改完要重启 astrbot。

**两份代码怎么改？**
`core/` 是真源，根目录是副本。改完跑 `python sync_core.py`；`python sync_core.py --check`
只比对（不一致退出码 1，可以挂 CI）。

## 目录

```
astrbot/
├── core/                源码真源：main / tiers / organizer / vectors + metadata + 配置 schema
├── main.py 等            core/ 的副本（安装器与插件市场从根目录读）
├── sync_core.py          core → 根目录 同步
├── ARCHITECTURE.md       架构（前半段与 dsh 逐字一致，后半段是本端适配）
├── CHANGELOG.md          改动笔记
└── test/                 测试（python test/test_xxx.py 直接跑）
```
