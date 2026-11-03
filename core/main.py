"""dsh-memory-tiers → AstrBot 协议桥（阶段 A）。

只做协议对接，不重写记忆逻辑：
- 每轮结束：把这一轮（用户 + 助手）追加进 ``L1.tmp/astrbot.jsonl``
- 每轮开始：读 ``L1.json`` + 所有端先的 temp，按 id 去重拼成注入块

整理（摘要 / 路由 / 遗忘 / 向量）留给 dsh。协议见
``dsh-memory-tiers/PORT_TO_ASTRBOT.md``。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # 4.x
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 老版本兜底
    TextPart = None  # type: ignore[assignment]

try:  # 作为包加载（开发目录 core/ 下有 __init__ 或按包导入）
    from .tiers import (
        MARK_CLOSE,
        MARK_OPEN,
        Tiers,
        line_of,
        sanitize_writer,
        wrap_block,
    )
    from .organizer import (
        CLASSIFY_BATCH_SYSTEM_PROMPT,
        RELATION_SYSTEM_PROMPT,
        Organizer,
        build_summary_prompt,
        parse_classify_batch,
        parse_relation,
        write_signal,
    )
    from .vectors import VectorStore
except ImportError:  # 作为独立插件目录加载（astrbot 插件市场安装后的形态）
    import os as _os
    import sys as _sys

    _HERE = _os.path.dirname(_os.path.abspath(__file__))
    if _HERE not in _sys.path:
        _sys.path.insert(0, _HERE)
    from tiers import (  # type: ignore[no-redef]
        MARK_CLOSE,
        MARK_OPEN,
        Tiers,
        line_of,
        sanitize_writer,
        wrap_block,
    )
    from organizer import (  # type: ignore[no-redef]
        CLASSIFY_BATCH_SYSTEM_PROMPT,
        RELATION_SYSTEM_PROMPT,
        Organizer,
        build_summary_prompt,
        parse_classify_batch,
        parse_relation,
        write_signal,
    )
    from vectors import VectorStore  # type: ignore[no-redef]

DEFAULTS = {
    "dir": "",
    "writer": "astrbot",
    "inject": True,
    "inject_target": "system_prompt",
    "context_keep": 0,
    "hot_max_chars": 3000,
    "hot_quota_own": 0.7,
    "long_max_chars": 3000,
    "consolidate_max_chars": 2500,
    "capture": True,
    "capture_user_chars": 2000,
    "capture_bot_chars": 400,
    "skip_commands": True,
    "organize": True,
    "organize_every_min": 5,
    "quiet_min": 10,
    "forget_threshold": 0.25,
    "l2_keep_max": 10,
    "summary_chunk": 60,
    "embed_model": "",
    "embed_chunk": 32,
    "semantic_inject": False,
    "semantic_top_k": 5,
    "semantic_min_sim": 0.35,
    "warn_layer_items": 200_000,
    "debug": False,
}

DEFAULT_REL = ".dsh/storages/memory-v5"
PLUGIN_DATA_SUBDIR = "memory"


def flatten_cfg(raw: Any) -> dict:
    """把分组配置铺平成一维。

    `_conf_schema.json` 里是 basic / inject / capture / organize / semantic / advanced
    六组（配置页按组折叠显示），而代码里统一按平铺的键读，所以进门前先摊平一次。
    老版本存的平铺配置也照样认：先摊平，顶层键再覆盖分组里的同名键。
    """
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if str(k).startswith("_"):
            continue
        if isinstance(v, dict):
            for kk, vv in v.items():
                if not str(kk).startswith("_"):
                    out[kk] = vv
        else:
            out[k] = v
    return out


def plugin_data_dir() -> str:
    """插件自己的数据目录（astrbot 惯例：data/plugin_data/<插件名>/）。

    默认记忆库就落在这里——两个插件各有一份，互不影响；
    只有把 `dir` 手动指到同一个目录，才真正共享一份记忆。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = get_astrbot_data_path()
    except Exception:
        base = os.path.join(os.getcwd(), "data")
    return os.path.join(base, "plugin_data", "astrbot_plugin_dsh_memory", PLUGIN_DATA_SUBDIR)


def resolve_dir(raw: Any) -> str:
    """数据目录优先级：配置 dir > 环境变量 MEMORY_TIERS_DIR > 插件自己的目录。

    留空 = 独立记忆库（从零初始化，和 dsh 无关）；
    填成 dsh 的目录（如 ~/.dsh/storages/memory-v5）= 两端共享同一份记忆。
    """
    s = str(raw or "").strip()
    if s:
        return os.path.abspath(os.path.expanduser(s))
    env = str(os.environ.get("MEMORY_TIERS_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(os.path.expanduser(plugin_data_dir()))


def _int(v: Any, dflt: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return dflt


def _float(v: Any, dflt: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _bool(v: Any, dflt: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "是")
    if v is None:
        return dflt
    return bool(v)


class DshMemoryBridge(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        raw = dict(config or {})
        cfg = {**DEFAULTS, **flatten_cfg(raw)}
        self.cfg = cfg

        self.writer = sanitize_writer(cfg.get("writer") or "astrbot")
        self.tiers = Tiers(
            dir_path=resolve_dir(cfg.get("dir")),
            writer=self.writer,
            hot_max_chars=_int(cfg.get("hot_max_chars"), 3000),
            hot_quota_own=_float(cfg.get("hot_quota_own"), 0.7),
            long_max_chars=_int(cfg.get("long_max_chars"), 3000),
            consolidate_max_chars=_int(cfg.get("consolidate_max_chars"), 2500),
        )
        self.inject = _bool(cfg.get("inject"), True)
        self.capture = _bool(cfg.get("capture"), True)
        self.target = str(cfg.get("inject_target") or "system_prompt").strip()
        # 对话历史截断：-1 不动 / 0 全清 / N 保留最近 N 轮
        self.context_keep = _int(cfg.get("context_keep"), 0)
        self.user_chars = max(0, _int(cfg.get("capture_user_chars"), 2000))
        self.bot_chars = max(0, _int(cfg.get("capture_bot_chars"), 400))
        self.skip_commands = _bool(cfg.get("skip_commands"), True)
        self.debug = _bool(cfg.get("debug"), False)

        # —— 阶段 C：自己整理 ——
        self.organize = _bool(cfg.get("organize"), True)
        self.organize_every_min = max(1, _int(cfg.get("organize_every_min"), 5))
        self.organizer = Organizer(
            self.tiers.dir,
            self.writer,
            {
                "hot_max_chars": self.tiers.hot_max_chars,
                "consolidate_max_chars": self.tiers.consolidate_max_chars,
                "long_max_chars": self.tiers.long_max_chars,
                "quiet_ms": max(1, _int(cfg.get("quiet_min"), 10)) * 60 * 1000,
                "forget_threshold": _float(cfg.get("forget_threshold"), 0.25),
                "l2_keep_max": max(1, _int(cfg.get("l2_keep_max"), 10)),
                "summary_chunk": max(1, _int(cfg.get("summary_chunk"), 60)),
                "embed_chunk": max(1, _int(cfg.get("embed_chunk"), 32)),
                "warn_layer_items": max(1, _int(cfg.get("warn_layer_items"), 200_000)),
            },
        )
        # —— 阶段 D：向量 ——
        self.embed_model = str(cfg.get("embed_model") or "").strip()
        self.semantic_inject = _bool(cfg.get("semantic_inject"), False)
        self.semantic_top_k = max(1, _int(cfg.get("semantic_top_k"), 5))
        self.semantic_min_sim = _float(cfg.get("semantic_min_sim"), 0.35)
        self._vec: VectorStore | None = None
        self._vec_probed = False

        self._org_busy = False
        self._org_loop: asyncio.Task | None = None
        self._org_tasks: set[asyncio.Task] = set()
        self._last_org: dict = {}

        # 每个会话最近一轮（event 对象上也有副本，这是兜底：event 钩子没走完时也能落盘）
        self._pending: dict[str, dict] = {}

        try:
            self.tiers.ensure_dirs()
            logger.info(
                f"[dsh_memory] 数据目录 {self.tiers.dir} writer={self.writer} "
                f"inject={self.inject}({self.target}) capture={self.capture}"
            )
        except OSError as e:
            logger.error(f"[dsh_memory] 数据目录不可用（{self.tiers.dir}）：{e}")

    # ==================== 向量（阶段 D） ====================

    def _embed_provider(self):
        """拿 astrbot 配的 embedding provider（通常就是 dsh 用的同一个 bge-m3）。"""
        ctx = self.context
        if ctx is None:
            return None
        pm = getattr(ctx, "provider_manager", None)
        insts = getattr(pm, "embedding_provider_insts", None) or []
        return insts[0] if insts else None

    def _embed_meta(self) -> tuple[str, int]:
        if self.embed_model:
            return self.embed_model, 0
        p = self._embed_provider()
        if p is None:
            return "", 0
        conf = getattr(p, "provider_config", {}) or {}
        model = str(conf.get("embedding_model") or conf.get("model") or "").strip()
        try:
            dim = int(p.get_dim())
        except Exception:
            dim = 0
        return model, dim

    def _vec_store(self) -> VectorStore | None:
        """惰性建库：模型名要从运行中的 provider 读，配置里拿不到就不启用。"""
        if self._vec is not None:
            return self._vec
        model, dim = self._embed_meta()
        if not model:
            if not self._vec_probed:
                self._vec_probed = True
                logger.info("[dsh_memory] 没找到 embedding provider，语义检索不启用")
            return None
        self._vec = VectorStore(self.tiers.dir, model, dim)
        ok, why = self._vec.writable()
        logger.info(
            f"[dsh_memory] 向量库 {self._vec.vec_path} sig={self._vec.sig} "
            f"{'可写' if ok else '只读（' + why + '）'}"
        )
        return self._vec

    async def _embed_batch(self, texts):
        p = self._embed_provider()
        if p is None or not texts:
            return []
        try:
            return await p.get_embeddings([str(t) for t in texts])
        except Exception as e:
            logger.warning(f"[dsh_memory] MODEL_FAIL embed {e}")
            return []

    async def _embed_one(self, text: str):
        p = self._embed_provider()
        if p is None or not str(text or "").strip():
            return None
        try:
            return await p.get_embedding(str(text)[:512])
        except Exception as e:
            logger.warning(f"[dsh_memory] MODEL_FAIL embed_one {e}")
            return None

    def _all_items(self) -> dict[str, dict]:
        by_id: dict[str, dict] = {}
        try:
            l1, _bad = self.tiers.read_l1_view()  # 含未合并的 temp
        except Exception:
            l1 = []
        for it in l1:
            by_id.setdefault(it.get("id"), it)
        for l in ("L2", "DL1", "DL2", "DL3"):
            for it in self.tiers.read_layer_safe(l):
                by_id.setdefault(it.get("id"), it)
        return by_id

    async def _search(self, query: str, k: int | None = None, depth: str = "auto") -> dict:
        """混合检索（关键词 + 向量 + 时间窗 + 分层），逻辑与 dsh 的 search 一致。"""
        store = self._vec_store()
        vectors = store.load() if store is not None else {}
        # 有 rerank provider 才精排；没有就明说没有，不用本地规则假装
        rerank = self._rerank if self._rerank_provider() is not None else None
        return await self.tiers.search(
            query,
            depth=depth,
            limit=k or self.semantic_top_k,
            vectors=vectors,
            embed_one=self._embed_one,
            rerank=rerank,
        )

    def _rerank_provider(self):
        """AstrBot 配了 rerank provider 就用；没有返回 None（检索会跳过精排）。"""
        ctx = self.context
        if ctx is None:
            return None
        pm = getattr(ctx, "provider_manager", None)
        if pm is None:
            return None
        for attr in ("rerank_provider_insts", "rerank_providers"):
            insts = getattr(pm, attr, None) or []
            if insts:
                return insts[0]
        return None

    async def _rerank(self, query: str, docs: list[str]):
        p = self._rerank_provider()
        if p is None or not docs:
            return []
        # 返回格式由 tiers.search 兼容（[{index, score}] 或 [float]）
        return await p.rerank(query, docs)

    async def _semantic(self, query: str, k: int | None = None) -> list[tuple[dict, float]]:
        """给注入用的形态：[(条目, 余弦)]。低于 semantic_min_sim 的不算命中。"""
        res = await self._search(query, k)
        if not res.get("hits"):
            return []
        by_id = self._all_items()
        out = []
        for h in res["hits"]:
            it = by_id.get(h.get("id"))
            if not it:
                continue
            # ★ 门槛用纯余弦（sem），不是混合分 score、也不是 rerank 的 rel：
            #   配置项叫"相似度下限"，就该按相似度算。
            sim = float(h.get("sem") or 0)
            if sim < self.semantic_min_sim:
                continue
            out.append((it, sim))
        return out

    # ==================== 整理（阶段 C） ====================


    async def _chat_text(self, provider, *, prompt, system_prompt, max_tokens=None, temperature=None):
        """调用 chat provider。**显式关掉推理**——这是 dsh 的 `thinking:false` 等价物。

        实测（硅基流动 + DeepSeek-V4.1-Flash）：不关的话一次摘要要 35 秒、
        3700 tokens，关掉之后 1.5 秒 / 1600 tokens。摘要是低频重任务，但也不能这么烧。

        astrbot 的 text_chat 会把「非默认参数」塞进 extra_body 直接发出去，
        所以 `enable_thinking=False` 能透传；个别模型不认这个参数时去掉再试一次。
        """
        args = {"prompt": prompt, "system_prompt": system_prompt, "contexts": []}
        if max_tokens:
            args["max_tokens"] = int(max_tokens)
        # 和 dsh 对齐：摘要 0.2、判关系 0。不传就会用 provider 默认值（常见 0.5~0.7），
        # 同一批原文在两端压出来的摘要会明显不同。
        if temperature is not None:
            args["temperature"] = float(temperature)
        args["enable_thinking"] = False
        try:
            return await provider.text_chat(**args)
        except Exception as e:
            msg = str(e)
            if "enable_thinking" in msg or "max_tokens" in msg or "temperature" in msg:
                for k in ("enable_thinking", "max_tokens", "temperature"):
                    args.pop(k, None)
                logger.info("[dsh_memory] 该模型不接受部分调用参数，退回默认调用")
                return await provider.text_chat(**args)
            raise

    async def _summarize(self, lines, channel, folks, hint):
        """把一轮原文交给 astrbot 当前使用的模型做摘要。

        返回原始文本（★/·/+ 行），解析交给 organizer，失败返回 None（整批回 L2）。
        """
        if self.context is None:
            return None
        try:
            provider = self.context.get_using_provider()
        except Exception as e:
            logger.warning(f"[dsh_memory] 取不到 provider：{e}")
            return None
        if provider is None:
            logger.warning("[dsh_memory] MODEL_MISSING summarize（没有可用的 chat provider）")
            return None
        sys_prompt, user_prompt = build_summary_prompt(lines, channel, folks, hint)
        try:
            resp = await self._chat_text(
                provider,
                prompt=user_prompt,
                system_prompt=sys_prompt,
                max_tokens=min(300 + 12 * len(lines), 4000),
                temperature=0.2,
            )
        except Exception as e:
            logger.warning(f"[dsh_memory] MODEL_FAIL summarize {e}")
            return None
        return getattr(resp, "completion_text", None)

    async def _judge_relation(self, a: str, b: str):
        """判两条摘要的关系（duplicate / contradiction / update / unrelated）。

        照 dsh 的 silicon.relation：只给模型两条文本，让它输出一个词。
        """
        if self.context is None:
            return None
        try:
            provider = self.context.get_using_provider()
        except Exception:
            return None
        if provider is None:
            logger.warning("[dsh_memory] MODEL_MISSING relation（没有可用的 chat provider）")
            return None
        try:
            resp = await self._chat_text(
                provider,
                prompt=f"A：{a}\nB：{b}",
                system_prompt=RELATION_SYSTEM_PROMPT,
                max_tokens=10,
                temperature=0.0,
            )
        except Exception as e:
            logger.warning(f"[dsh_memory] MODEL_FAIL relation {e}（未清洗，两条都留）")
            return None
        return parse_relation(getattr(resp, "completion_text", "") or "")

    async def _classify_batch(self, texts: list[str]) -> list[dict | None]:
        """批量分类：给每条记忆判 channel（dialog/action/event）与 importance。

        一次请求打一整批（照 dsh 的 `classifyBatch`），不是逐条往返。
        按"模型是硬性要求"的原则只走模型，不用本地正则假装分类过；
        整批失败返回全 None，调用方会保留 impRough，下一轮再试。
        """
        n = len(texts)
        if not n:
            return []
        if self.context is None:
            logger.warning("[dsh_memory] MODEL_MISSING classify（没有 context）")
            return [None] * n
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None:
            logger.warning("[dsh_memory] MODEL_MISSING classify（没有可用的 chat provider）")
            return [None] * n

        bodies = [str(t or "").strip()[:400] for t in texts]
        payload = "\n".join(f"{i + 1}. {b}" for i, b in enumerate(bodies))
        try:
            resp = await self._chat_text(
                provider,
                prompt=payload,
                system_prompt=CLASSIFY_BATCH_SYSTEM_PROMPT,
                max_tokens=200 + n * 60,
                temperature=0.0,
            )
        except Exception as e:
            logger.warning(f"[dsh_memory] MODEL_FAIL classify n={n} {e}")
            return [None] * n
        got = parse_classify_batch(getattr(resp, "completion_text", "") or "", bodies)
        if all(x is None for x in got):
            logger.warning(f"[dsh_memory] MODEL_FAIL classify n={n} 返回无法解析")
        return got

    def _has_chat_provider(self) -> bool:
        """现在有没有可用的对话模型。没有就传 None，让整理层走「下沉 DL1」而不是「失败回 L2」。"""
        if self.context is None:
            return False
        try:
            return self.context.get_using_provider() is not None
        except Exception:
            return False

    async def _organize_once(self, reason: str = "") -> None:
        if not self.organize or self._org_busy:
            return
        self._org_busy = True
        try:
            self.organizer.vectors = self._vec_store()
            # ★ 能力检测必须在整理层做：__summarize 永远是绑定方法（非 None），
            #   不在这里判，provider 缺失就会被当成"模型调用失败"而回 L2。
            summarize = self._summarize if self._has_chat_provider() else None
            if summarize is None:
                logger.warning("[dsh_memory] MODEL_MISSING summarize：本轮只下沉 DL1，不产生摘要")
            res = await self.organizer.organize(
                summarize, self._embed_batch, self._judge_relation,
                self._classify_batch,
            )
            self._last_org = res
            if res.get("ok"):
                if self.debug:
                    logger.info(
                        f"[dsh_memory] 整理完成({reason}) merged={res.get('merged')} "
                        f"L1={res.get('l1Moved')} L2={res.get('l2Moved')} "
                        f"forget={res.get('forgotten')} vec={res.get('vectors')} L3={res.get('l3')}"
                    )
            elif res.get("skipped") not in ("busy", "no_model"):
                logger.warning(f"[dsh_memory] 整理未完成：{res}")
        except Exception as e:
            logger.warning(f"[dsh_memory] 整理异常：{e}")
        finally:
            self._org_busy = False

    def _spawn_organize(self, reason: str) -> None:
        """后台跑，别拖慢回复。"""
        if not self.organize or self._org_busy:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._organize_once(reason))
        self._org_tasks.add(task)
        task.add_done_callback(self._org_tasks.discard)

    async def _organize_loop(self) -> None:
        """定时补漏：dsh 不在线时，安静下来的轮次靠这个沉淀。"""
        interval = self.organize_every_min * 60
        while True:
            await asyncio.sleep(interval)
            await self._organize_once("timer")

    def _ensure_loop(self) -> None:
        if not self.organize or self._org_loop is not None:
            return
        try:
            self._org_loop = asyncio.get_running_loop().create_task(self._organize_loop())
        except RuntimeError:
            self._org_loop = None

    # ==================== 事件钩子 ====================

    @filter.on_llm_request()
    async def on_request(self, event: AstrMessageEvent, req) -> None:
        """先把上一条没落盘的兜底写掉，再注入记忆，最后起这一轮。"""
        try:
            self._ensure_loop()
            self._sweep_session(event)
            if self.inject:
                try:
                    query = str(event.get_message_str() or "")
                except Exception:
                    query = ""
                await self._inject(req, query)
            if self.capture:
                self._begin_turn(event)
        except Exception as e:  # 记忆坏了不能把聊天搞坏
            logger.warning(f"[dsh_memory] on_llm_request 失败：{e}")

    @filter.on_llm_response()
    async def on_response(self, event: AstrMessageEvent, resp) -> None:
        try:
            turn = event.get_extra("_dsm_turn")
            if not isinstance(turn, dict):
                return
            text = getattr(resp, "completion_text", None)
            if not isinstance(text, str):
                return
            text = text.strip()
            if not text:
                return
            # 不在这里截断：dsh 的 botCaptureChars 是「整轮拼起来再截」，
            # 工具循环里会有多条 assistant 正文，逐条截会记下好几段、总量远超上限。
            if turn.get("last_bot") == text:
                return  # 同一段回复被多次回调，别重复记
            turn["last_bot"] = text
            turn["rows"].append(
                {
                    "text": text,
                    "speaker": "bot",
                    "channel": "dialog",
                    "scene": turn.get("scene", ""),
                    "people": [],
                }
            )
        except Exception as e:
            logger.warning(f"[dsh_memory] on_llm_response 失败：{e}")

    @filter.after_message_sent()
    async def on_sent(self, event: AstrMessageEvent, *_args) -> None:
        """正常出口：这一轮真的发出去了，整轮落盘。"""
        try:
            turn = event.get_extra("_dsm_turn")
            if isinstance(turn, dict):
                self._flush(turn)
                self._spawn_organize("turn")
        except Exception as e:
            logger.warning(f"[dsh_memory] on_sent 失败：{e}")

    async def terminate(self) -> None:
        if self._org_loop is not None:
            self._org_loop.cancel()
            self._org_loop = None
        for t in list(self._org_tasks):
            t.cancel()
        self._org_tasks.clear()
        for turn in list(self._pending.values()):
            self._flush(turn)
        self._pending.clear()

    # ==================== 注入 ====================

    def _trim_contexts(self, req) -> None:
        """截短 astrbot 自带的对话历史（`req.contexts`）。

        为什么：记忆系统本身已经存了原话（L1 即时层），历史再留一份属于重复；
        而且历史越长，注入块之后的缓存段就越大。

        `context_keep_turns`：
          -1 = 不动（保留 astrbot 原样）
           0 = 全清（只留开头的 system 消息）
           N = 只留最近 N 轮（一轮按 user+assistant 两条估算）

        ★ 缓存提示：keep=0 时前缀 = system+记忆+本轮消息，历史不再参与，
          是缓存最稳的形态；keep=N 时窗口每轮滑动、"最近 N 轮"的第一个元素在变，
          前缀反而不稳定。要么不截，要么全清，中间档省的是 token 不是缓存。
        """
        keep = int(getattr(self, "context_keep", -1))
        if keep < 0:
            return
        ctxs = getattr(req, "contexts", None)
        if not isinstance(ctxs, list) or not ctxs:
            return
        # 开头的 system 消息不属于"对话历史"，保留
        head: list = []
        body = ctxs
        if body and isinstance(body[0], dict) and body[0].get("role") == "system":
            head, body = [body[0]], body[1:]
        if keep == 0:
            req.contexts = head
            return
        n = keep * 2
        if len(body) > n:
            req.contexts = head + body[-n:]

    async def _inject(self, req, query: str = "") -> None:
        # 先截对话历史：记忆系统已经记了原话，这里再留一份是重复的。
        # 截断必须在注入之前，顺序才对得上「system + 记忆 + 剩下的历史 + 本轮消息」。
        self._trim_contexts(req)
        block = self.tiers.build_block()
        if self.semantic_inject and query:
            try:
                hits = await self._semantic(query)
            except Exception as e:
                logger.warning(f"[dsh_memory] 语义检索失败：{e}")
                hits = []
            if hits:
                lines = ["## 相关的旧事"]
                seen: set[str] = set()
                for it, sim in hits:
                    line = line_of(it)
                    if line in seen:
                        continue
                    seen.add(line)
                    lines.append(f"{line}（{sim:.2f}）")
                extra = "\n".join(lines)
                # build_block 已经套好外壳，语义段要插在闭合标签之前
                if block:
                    block = block.replace(f"\n{MARK_CLOSE}", f"\n{extra}\n{MARK_CLOSE}")
                else:
                    block = wrap_block(extra)
        if not block:
            return
        # build_block / wrap_block 已经带好外壳与「这是记忆」的说明
        text = block

        if self.target == "system_prompt":
            # 拼到系统提示词尾部 = 「系统提示 + 工具/技能 + 人格 + 记忆 + 对话」，
            # 这里拿到的 system_prompt 已经含工具与人格（插件只能往后加）。
            cur = getattr(req, "system_prompt", "") or ""
            if MARK_OPEN in cur:
                return
            try:
                req.system_prompt = f"{cur}\n\n{text}" if cur else text
            except Exception as e:
                logger.warning(f"[dsh_memory] 写 system_prompt 失败：{e}")
                return
        elif self.target == "contexts_user":
            ctxs = getattr(req, "contexts", None)
            if not isinstance(ctxs, list):
                return
            if ctxs and isinstance(ctxs[-1], dict) and MARK_OPEN in str(ctxs[-1].get("content")):
                return
            # 同样必须标临时：contexts 会被写回会话历史，不标就会一轮轮堆下去
            ctxs.append({"role": "user", "content": text, "_no_save": True})
        else:  # extra_user_part：挂在用户消息后面
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                return
            for p in parts:
                if MARK_OPEN in str(getattr(p, "text", "") or ""):
                    return
            if TextPart is None:
                # dict 形式：靠 _no_save 让历史落库时丢掉这一块
                parts.append({"type": "text", "text": text, "_no_save": True})
            else:
                # ★ 必须 mark_as_temp()。
                #   不加的话 astrbot 会把这块**持久化进会话历史**：
                #   ① 每轮往历史里塞一份 4000 字记忆，十轮后上下文被记忆灌满
                #   ② 下一轮模型会同时看到「上轮的旧记忆」和「本轮的新记忆」，
                #      旧的那份可能已经过时，两者冲突时模型无从判断
                #   ③ 历史里那一段每轮都变，等于把前缀缓存整段作废
                parts.append(TextPart(text=text).mark_as_temp())

        if self.debug:
            logger.info(f"[dsh_memory] 注入 {len(block)} 字")

    # ==================== 抓取 ====================

    def _scene(self, event: AstrMessageEvent) -> str:
        platform = ""
        try:
            platform = event.get_platform_name() or ""
        except Exception:
            platform = ""
        try:
            gid = event.get_group_id()
        except Exception:
            gid = ""
        if gid:
            name = gid
            grp = getattr(getattr(event, "message_obj", None), "group", None)
            gname = getattr(grp, "group_name", None) if grp is not None else None
            if gname:
                name = str(gname)
            return f"{platform}·{name}" if platform else name
        return f"{platform}·私聊" if platform else "私聊"

    def _sender(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_name() or "").strip()
        except Exception:
            return ""

    def _begin_turn(self, event: AstrMessageEvent) -> None:
        if isinstance(event.get_extra("_dsm_turn"), dict):
            return  # 同一条消息的第二次 LLM 调用（工具循环），复用同一轮

        text = ""
        try:
            text = str(event.get_message_str() or "").strip()
        except Exception:
            text = ""
        if not text:
            return
        if self.skip_commands and text.startswith(("/", "／")):
            return
        if self.user_chars > 0:
            text = text[: self.user_chars]

        scene = self._scene(event)
        who = self._sender(event)
        rows = [
            {
                "text": text,
                "speaker": "user",
                "channel": "dialog",
                "scene": scene,
                "people": [who] if who else [],
            }
        ]
        turn = {
            "owner": id(event),
            "rows": rows,
            "scene": scene,
            "flushed": False,
            "last_bot": None,
            "turn_id": "",
        }
        event.set_extra("_dsm_turn", turn)
        umo = ""
        try:
            umo = str(event.unified_msg_origin)
        except Exception:
            umo = ""
        if umo:
            self._pending[umo] = turn

    def _flush(self, turn: dict) -> None:
        if turn.get("flushed"):
            return
        turn["flushed"] = True
        rows = [r for r in turn.get("rows") or [] if str(r.get("text") or "").strip()]
        # 整轮的 bot 正文拼成一条再截（与 dsh 的整轮口径一致）
        bots = [r for r in rows if r.get("speaker") == "bot"]
        if bots and self.bot_chars > 0:
            joined = "\n".join(str(r.get("text") or "") for r in bots).strip()
            rows = [r for r in rows if r.get("speaker") != "bot"]
            if joined:
                row = dict(bots[0])
                row["text"] = joined[: self.bot_chars]
                rows.append(row)
        umo = None
        for k, v in list(self._pending.items()):
            if v is turn:
                umo = k
                break
        if umo:
            self._pending.pop(umo, None)
        if not rows:
            return
        res = self.tiers.write_turn(rows, turn.get("turn_id") or None)
        if res.get("ok"):
            turn["turn_id"] = res["turnId"]
            if self.debug:
                logger.info(
                    f"[dsh_memory] 落盘 {res['turnId']} {len(res['ids'])} 条 → {self.tiers.temp_path}"
                )
        else:
            logger.warning(f"[dsh_memory] 落盘失败：{res.get('error')}")

    def _sweep_session(self, event: AstrMessageEvent) -> None:
        """上一条消息若因异常没走到 after_message_sent，这里补一刀（不丢对话）。"""
        try:
            umo = str(event.unified_msg_origin)
        except Exception:
            return
        old = self._pending.get(umo)
        if old is None or old is event.get_extra("_dsm_turn"):
            return
        if old.get("owner") != id(event) and not old.get("flushed"):
            self._flush(old)

    # ==================== 命令 ====================

    @filter.command("memory")
    async def memory_cmd(self, event: AstrMessageEvent):
        """记忆命令入口：/memory（状态）或 /memory help 看全部子命令。

        命令名与 dsh 的工具名 `memory_*` 对齐，不带任何一端的标识。
        """
        async for r in self._mem_cmd(event):
            yield r

    @filter.command("dsm")
    async def dsm_cmd(self, event: AstrMessageEvent):
        """旧命令名，保留兼容（等价 /memory）。"""
        async for r in self._mem_cmd(event):
            yield r

    async def _mem_cmd(self, event: AstrMessageEvent):
        arg = ""
        rest = ""
        try:
            parts = str(event.get_message_str() or "").split()
            arg = parts[1].lower() if len(parts) > 1 else ""
            rest = " ".join(parts[2:]).strip()
        except Exception:
            arg = ""

        if arg in ("help", "帮助", "?", "？"):
            yield event.plain_result(
                "共享记忆命令（/dsm 与 /memory 等价）：\n"
                "/memory              看状态（各层条数、向量库、整理情况）\n"
                "/memory write 内容    手动记一条（channel/importance 由整理时分类补）\n"
                "/memory search 关键词  跨五层检索（结果带 id，可直接复制进下面的命令）\n"
                "/memory attend <id...> 关注：热度+1，DL1 的会回 L2 重新观察\n"
                "/memory forget <id...> 忘掉：写台账后从库和向量里删掉\n"
                "/memory pin <id...>    钉住：不参与遗忘和淘汰\n"
                "/memory unpin <id...>  取消钉住\n"
                "/memory ctx          看当前注入的上下文（分段统计）\n"
                "/memory ctx l3|l2|l1|sem   看某一段全文（限 3000 字）\n"
                "/memory ctx all      看整块（截断 3000 字）\n"
                "/memory ctx file     导出整块到文件（内容长时用这个）\n"
                "/memory org          立刻整理一次（并入/搬运/遗忘/补向量）"
            )
            return

        if arg in ("write", "记"):
            if not rest:
                yield event.plain_result("用法：/memory write 要记的内容")
                return
            r = self.tiers.write_turn([{
                "text": rest,
                "speaker": "user",
                "channel": "dialog",
                "scene": "手动",
            }])
            if r.get("ok"):
                n = len(r.get("ids") or [])
                yield event.plain_result(
                    f"已记下 {n} 条（进即时层，下次整理时补 channel/importance 再归层）"
                )
            else:
                yield event.plain_result(f"没记成：{r.get('error') or r}")
            return

        if arg in ("search", "搜"):
            if not rest:
                yield event.plain_result("用法：/dsm search 要搜的内容")
                return
            res = await self._search(rest, k=8)
            hits = res.get("hits") or []
            if not hits:
                yield event.plain_result(
                    "没搜到。可能还没建向量库，先 /dsm org 整理一次把向量补齐。"
                )
                return
            out = [
                f"检索「{rest}」（{res.get('depth')}，confidence {res.get('confidence')}）："
            ]
            for h in hits:
                tag = f"[{h.get('layer')}] "
                via = f" ↳via {h.get('via')}" if h.get("via") else ""
                rel = h.get("rel")
                score = f"{rel:.2f}" if rel is not None else f"{h.get('score'):.2f}"
                out.append(f"- {tag}{h.get('id')} {str(h.get('text'))[:50]}（{score}）{via}")
            out.append(res.get("advice") or "")
            yield event.plain_result("\n".join(out)[:1800])
            return

        if arg in ("ctx", "上下文"):
            info = self.tiers.inspect_block()
            if not info["block"]:
                yield event.plain_result("共享记忆里还没有可注入的内容。")
                return
            sub = rest.strip().lower()
            names = {
                "l3": "一直记得的", "记得": "一直记得的",
                "l2": "最近沉淀的", "沉淀": "最近沉淀的",
                "l1": "刚发生的", "刚发生": "刚发生的",
                "sem": "相关的旧事", "旧事": "相关的旧事",
            }

            # 导出文件：内容太长时看文件比看聊天窗口方便
            if sub in ("file", "导出"):
                path = os.path.join(self.tiers.dir, "ctx_dump.txt")
                try:
                    with open(path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(info["block"])
                    yield event.plain_result(
                        f"已导出 {info['total_chars']} 字到：\n{path}"
                    )
                except OSError as e:
                    yield event.plain_result(f"导出失败：{e}")
                return

            # 看某一段的全部
            if sub in names:
                sec = next((s for s in info["sections"] if s["name"] == names[sub]), None)
                if not sec or not sec["lines"]:
                    yield event.plain_result(f"「{names[sub]}」这一段现在是空的。")
                    return
                body = "\n".join(sec["lines"])
                head = f"「{sec['name']}」{sec['count']} 条 / {sec['chars']} 字\n"
                limit = 3000
                text = head + body
                if len(text) > limit:
                    text = text[:limit] + f"\n…（还有 {len(body) - limit + len(head)} 字没显示，用 /dsm ctx file 导出全文）"
                yield event.plain_result(text)
                return

            # 看全文（默认就截断，避免刷屏）
            if sub in ("all", "全"):
                block = info["block"]
                limit = 3000
                text = block if len(block) <= limit else (
                    block[:limit] + f"\n…（共 {len(block)} 字，已截断；用 /dsm ctx file 导出全文）"
                )
                yield event.plain_result(text)
                return

            # 总览：每段条数 + 字数 + 一条示例
            out = [f"注入块 {info['total_chars']} 字"]
            if info["hidden_l1"]:
                out[0] += f"（即时层还有 {info['hidden_l1']} 条因容量没显示）"
            for s in info["sections"]:
                if not s["lines"]:
                    out.append(f"- {s['name']}：空")
                    continue
                sample = s["lines"][0]
                sample = sample if len(sample) <= 40 else sample[:40] + "…"
                out.append(f"- {s['name']}：{s['count']} 条 / {s['chars']} 字｜{sample}")
            out.append("")
            out.append("看某段全文：/dsm ctx l3｜l2｜l1｜sem")
            out.append("看整块（截断 3000 字）：/dsm ctx all")
            out.append("导出全文到文件：/dsm ctx file")
            yield event.plain_result("\n".join(out)[:1800])
            return

        if arg in ("peek", "看"):
            block = self.tiers.build_block()
            if not block:
                yield event.plain_result("共享记忆里还没有可注入的内容。")
                return
            yield event.plain_result(block[:1500])
            return

        if arg in ("org", "整理"):
            if not self.organize:
                yield event.plain_result("整理是关着的（配置里 organize=false）。")
                return
            if self._org_busy:
                yield event.plain_result("正在整理中，等这轮结束再试。")
                return
            await self._organize_once("manual")
            r = self._last_org or {}
            if r.get("ok"):
                yield event.plain_result(
                    "整理完成："
                    f"并入 {r.get('merged', 0)} 条，"
                    f"L1→L2 {r.get('l1Moved', 0)} 条，"
                    f"L2 摘要 {r.get('l2Moved', 0)} 条，"
                    f"遗忘 {r.get('forgotten', 0)} 条，"
                    f"L3 {r.get('l3', 0)} 条，{r.get('elapsedMs', 0)}ms\n"
                    f"层内条数：{r.get('count')}"
                )
            else:
                yield event.plain_result(f"整理没做：{r.get('skipped') or r.get('error') or r}")
            return

        if arg in ("attend", "关注"):
            ids = rest.split()
            if not ids:
                yield event.plain_result("用法：/dsm attend <id...>（id 从 /dsm search 的结果里拿）")
                return
            rec = write_signal(self.tiers.dir, self.writer, "attend", ids)
            yield event.plain_result(
                f"已提交关注 {len(rec['ids'])} 条，下次整理生效"
                "（热度 +1；DL1 的会回到 L2 重新观察）"
            )
            return

        if arg in ("pin", "钉住"):
            ids = rest.split()
            if not ids:
                yield event.plain_result("用法：/dsm pin <id...>")
                return
            rec = write_signal(self.tiers.dir, self.writer, "pin", ids, True)
            yield event.plain_result(f"已提交钉住 {len(rec['ids'])} 条，下次整理生效")
            return

        if arg in ("unpin", "取消钉住"):
            ids = rest.split()
            if not ids:
                yield event.plain_result("用法：/dsm unpin <id...>")
                return
            rec = write_signal(self.tiers.dir, self.writer, "pin", ids, False)
            yield event.plain_result(f"已提交取消钉住 {len(rec['ids'])} 条，下次整理生效")
            return

        if arg in ("forget", "忘掉"):
            ids = rest.split()
            if not ids:
                yield event.plain_result("用法：/dsm forget <id...>（先写 graveyard 台账再删，不可恢复）")
                return
            rec = write_signal(self.tiers.dir, self.writer, "forget", ids)
            yield event.plain_result(
                f"已提交遗忘 {len(rec['ids'])} 条，下次整理生效（台账写不进去就不会删）"
            )
            return

        st = self.tiers.stats()
        org_state = self.organizer.state()
        vec = self._vec_store()
        vec_line = "向量：未启用（没找到 embedding provider）"
        if vec is not None:
            vs = vec.stats()
            vec_line = (
                f"向量：{vs['count']} 条 / sig {vs['sig']} / dim {vs['dim']}"
                f"{'' if vs['writable'] else '（只读：' + vs['reason'] + '）'}"
            )
        org_line = "整理：关"
        if self.organize:
            busy = "，正在整理" if self._org_busy else ""
            org_line = f"整理：开（每 {self.organize_every_min} 分钟）{busy}"
            if org_state:
                org_line += (
                    f"\n上次整理：并入 {org_state.get('merged', 0)}，"
                    f"L1→L2 {org_state.get('l1Moved', 0)}，"
                    f"L2 {org_state.get('l2Moved', 0)}，"
                    f"遗忘 {org_state.get('forgotten', 0)}，"
                    f"L3 {org_state.get('l3', 0)}"
                )
        lines = [
            "dsh 记忆桥（阶段 A+B+C+D）",
            f"目录：{st['dir']}",
            f"writer：{st['writer']}",
            f"自己的 temp：{'有' if st['temp_exists'] else '还没有'}（{st['temp']}）",
            f"层容量：L1 视图 {st['l1']} 条 / L2 {st['l2']} 条 / L3 {st['l3']} 条",
            "来源分布：" + ("，".join(f"{k}={v}" for k, v in st["by_origin"].items()) or "无"),
            f"坏行：{st['bad_lines']}",
            f"注入：{'开' if self.inject else '关'}（{self.target}） 抓取：{'开' if self.capture else '关'}",
            f"语义注入：{'开' if self.semantic_inject else '关'}（top {self.semantic_top_k}，余弦 ≥ {self.semantic_min_sim}）",
            f"精排：{'有 rerank provider' if self._rerank_provider() is not None else '无（advice 按综合分标定）'}",
            vec_line,
            org_line,
            f"层缓存：命中 {st.get('cache', {}).get('hits', 0)} / "
            f"未命中 {st.get('cache', {}).get('misses', 0)}",
        ]
        # 容量告警：不改行为，只提示该整理了
        lay = st.get("layers") or {}
        warn_bits = [f"{k}={v}" for k, v in lay.items() if v > 200_000]
        if warn_bits:
            lines.append("⚠️ 层容量：" + " ".join(warn_bits)
                         + "（超过 20 万条，建议 /dsm org 看整理是否卡住）")
        if st.get("l1", 0) > 500_000:
            lines.append(f"⚠️ 即时层（含 temp）{st['l1']} 条，注入只取头部；建议尽快整理")
        yield event.plain_result("\n".join(lines))
