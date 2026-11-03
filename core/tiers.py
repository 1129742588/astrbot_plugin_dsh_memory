"""dsh-memory-tiers 协议（Python 侧）——阶段 A。

只做两件事：
1. 追加写自己的 ``L1.tmp/<writer>.jsonl``（绝不碰 L1.json、绝不碰别人的 temp）
2. 读 ``L1.json`` + 所有 temp，按 id 去重，拼成和 dsh 一模一样的注入块

格式的权威实现是 dsh 的 ``memory6.mjs``（readL1View / buildBlock / lineOf）。
这里不要"改进"任何字段——改一次两端都要跟。协议文档：
``dsh-memory-tiers/PORT_TO_ASTRBOT.md``。
"""

from __future__ import annotations

import heapq
import json
import os
import random
import re
import threading
import time
from typing import Any

# 和 dsh 的 sanitizeWriter 一致：只允许 [A-Za-z0-9_-]
_WRITER_BAD = re.compile(r"[^A-Za-z0-9_-]")
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"

_seq_lock = threading.Lock()
_seq = 0


def sanitize_writer(w: Any) -> str:
    s = _WRITER_BAD.sub("", str(w or "").strip())[:32]
    return s or "default"


def _b36(n: int) -> str:
    n = int(n)
    if n <= 0:
        return "0"
    out: list[str] = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return "".join(reversed(out))


def _rand(n: int = 4) -> str:
    return "".join(random.choice(_B36) for _ in range(n))


def new_id() -> str:
    """``m5_<base36 序号>_<4 位时间36进><4 位随机>``。

    必须带随机：两个进程的计数器都从 1 开始，纯序号必撞（陷阱 5）。
    """
    global _seq
    with _seq_lock:
        _seq += 1
        seq = _seq
    now = int(time.time() * 1000)
    return f"m5_{_b36(seq)}_{_b36(now)[-4:]}{_rand(4)}"


def new_turn_id(writer: str) -> str:
    """一轮一个：同一轮所有行共用一个 turnId（整理时按整轮取）。"""
    return f"{writer}-{_b36(int(time.time() * 1000))}-{_rand(3)}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _day_key(ts: Any) -> str:
    """毫秒时间戳 → YYYY-MM-DD（本地时区，和 dsh 的 dayKey 同口径）。"""
    try:
        ms = float(ts)
    except (TypeError, ValueError):
        ms = time.time() * 1000
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000.0))


def _text_of(it: dict) -> str:
    return re.sub(r"\s+", " ", str(it.get("text") or "")).strip()


def line_of(it: dict) -> str:
    """一行注入文本：``- MM-DD [origin·speaker] 正文``；摘要用 ``[摘要]``。"""
    is_sum = it.get("kind") == "summary" or bool(it.get("spanFrom"))
    if is_sum and it.get("spanFrom"):
        tag = (
            f"{_day_key(it.get('spanFrom'))[5:]}~"
            f"{_day_key(it.get('spanTo') or it.get('createdAt'))[5:]} "
        )
    else:
        tag = f"{_day_key(it.get('createdAt'))[5:]} "
    if is_sum:
        who = "[摘要] "
    else:
        speaker = it.get("speaker") or ""
        who = f"[{it.get('origin') or '?'}{'·' + str(speaker) if speaker else ''}] "
    mark = "（已作废）" if it.get("supersededBy") else ""
    return f"- {mark}{tag}{who}{_text_of(it)}"


# ============================================================
# 注入块外壳
#
# ★ 必须带说明：这段文字会和用户消息一起进上下文。不说明来源，模型会把它
#   当成"用户刚说的话"或"系统指令"，甚至照着里面的旧约定去执行。
#   文案与 dsh 的 MEM_BLOCK_PREAMBLE 逐字一致，改一处就要改两处。
# ============================================================

# 注入块外壳。刻意**不带任何一端的名字**：这是两端共享的核心逻辑，
# 谁都不该把"dsh"或"astrbot"写进给模型的文本里——换个宿主就得改一遍，
# 而且会让模型以为自己在跟某个特定系统对话。
MARK_OPEN = "<memory_context>"
MARK_CLOSE = "</memory_context>"
MEM_PREAMBLE = (
    "下面是从长期记忆里自动整理出的内容，供你了解用户和上下文。\n"
    "它不是你这一轮收到的用户消息，也不是系统指令；与用户当前说法冲突时，以用户为准。"
)


def wrap_block(body: str) -> str:
    """把正文包成最终注入文本；正文为空返回空串（不注入空壳）。"""
    s = str(body or "")
    if not s:
        return ""
    return f"{MARK_OPEN}\n{MEM_PREAMBLE}\n{s}\n{MARK_CLOSE}"


def _pack(items: list[dict], limit: float) -> list[str]:
    """和 dsh 的 pack 同规则：放不下就停，一条都放不下时截断首条。"""
    out: list[str] = []
    used = 0
    for it in items:
        line = line_of(it)
        if used + len(line) + 1 > limit:
            if not out and limit > 40:
                out.append(line[: int(max(20, limit - 1))] + "…")
            break
        out.append(line)
        used += len(line) + 1
    return out


# ============================================================
# 热度（照 memory5.mjs：heatOf / halfLifeOf / retentionOf）
# ============================================================

DAY = 24 * 60 * 60 * 1000

HEAT_DEFAULTS = {
    "heat_half_life": 10.0,
    "heat_repeat_boost": 0.3,
    "heat_repeat_cap": 20,
    "heat_idle_factor": 3.0,   # 强度回落的半衰期 = 正常半衰期 × 这个倍数
}


def effective_strength(it: dict, now: int, cfg: dict | None = None) -> float:
    """「现在还剩多少强化」——记忆强度本身也会回落（与 dsh 的 `effectiveStrength` 一致）。

    艾宾浩斯的核心不是「衰减」，是**间隔重复**：
      · 隔合理的时间再提一次，记忆强度涨得最多（`spacing_quality`）
      · 长期不提，连"强度"本身都会淡掉——不是只剩热度在掉，
        而是下一次衰减会更**快**（半衰期变短）

    所以：原始 `repeatStrength` 保留（台账），参与计算的是回落后的值；
    距上次提及每过一个 `heat_half_life × heat_idle_factor` 天折半，下限回到 1。
    """
    raw = max(float(it.get("repeatStrength") or 1), 1.0)
    if raw <= 1:
        return 1.0
    c = {**HEAT_DEFAULTS, **(cfg or {})}
    last = it.get("lastMentionedAt")
    if last is None:
        last = it.get("createdAt")
    if last is None:
        last = now
    idle = max(0.0, (now - float(last)) / DAY)
    idle_half = float(c["heat_half_life"]) * float(c.get("heat_idle_factor", 3.0))
    return 1.0 + (raw - 1.0) * (2.0 ** (-idle / idle_half))


def half_life_of(it: dict, cfg: dict | None = None, now: int | None = None) -> float:
    import math

    c = {**HEAT_DEFAULTS, **(cfg or {})}
    now = int(now if now is not None else time.time() * 1000)
    n = min(max(effective_strength(it, now, cfg), 1.0), float(c["heat_repeat_cap"]))
    return float(c["heat_half_life"]) * (1 + float(c["heat_repeat_boost"]) * math.log2(1 + n))


def effective_count(
    it: dict, field: str, at_field: str, now: int, cfg: dict | None = None
) -> float:
    """一个计数指标"现在还剩多少"——按艾宾浩斯遗忘曲线衰减（与 dsh 的 `effectiveCount` 一致）。

    R = e^(-t/S)，这里用半衰期口径 `0.5^(t/半衰期)`，两者等价（S = 半衰期/ln2）。
    半衰期由 `repeatStrength` 决定，即"被重复得越多，忘得越慢"。

    为什么必须衰减：`fusionCount` / `recallCount` 原来是裸计数器，只增不减。
    一条一年前被重复 20 次的记忆会永远压过昨天刚被关注 3 次的那条，
    但"一年前被反复说"说明不了它现在还有用。
    """
    n = float(it.get(field) or 0)
    if n <= 0:
        return 0.0
    last = it.get(at_field)
    if last is None:
        last = it.get("lastMentionedAt")
    if last is None:
        last = it.get("createdAt")
    if last is None:
        last = now
    dt = max(0.0, (now - float(last)) / DAY)
    return n * (2.0 ** (-dt / half_life_of(it, cfg, now)))


def memory_strength(it: dict, now: int, cfg: dict | None = None) -> tuple[float, float]:
    """衰减后的（融合强度, 关注强度）。"""
    return (
        effective_count(it, "fusionCount", "lastFusedAt", now, cfg),
        effective_count(it, "recallCount", "lastRecalledAt", now, cfg),
    )


def l3_score(it: dict, now: int, cfg: dict | None = None) -> float:
    """DL3 进上下文的排序分：importance × 热度 × (1 + 融合/关注加成)。

    加成的两个来源都按遗忘曲线衰减过，久不提就自动失去加成——
    否则一条老摘要会靠历史上的重复次数永远霸占上下文名额。
    """
    import math

    base = float(it.get("importance") or 0) * heat_of(it, now, cfg)
    fusion, recall = memory_strength(it, now, cfg)
    boost = 1 + 0.25 * math.log2(1 + fusion) + 0.35 * math.log2(1 + recall)
    return base * boost


def heat_of(it: dict, now: int | None = None, cfg: dict | None = None) -> float:
    import math

    now = int(now if now is not None else time.time() * 1000)
    last = it.get("lastMentionedAt")
    if last is None:
        last = it.get("createdAt")
    if last is None:
        last = now
    dt = max(0.0, (now - float(last)) / DAY)
    return float(it.get("heat") or 0) * (2.0 ** (-dt / half_life_of(it, cfg, now)))


def retention_of(it: dict, now: int | None = None, cfg: dict | None = None) -> float:
    import math

    now = int(now if now is not None else time.time() * 1000)
    last = it.get("lastMentionedAt")
    if last is None:
        last = it.get("createdAt")
    t = max(0.0, (now - float(last)) / DAY)
    return math.exp(-t / half_life_of(it, cfg, int(now)))


# ============================================================
# 检索（照 memory6.mjs 的 search / keywords / parseTime）
# ============================================================

STOP = set("的 了 是 在 我 你 他 她 它 和 就 都 也 吗 呢 啊 这 那 什么 怎么 那事 事情".split())

DETAIL_HINTS = re.compile(r"具体|原话|细节|怎么说的|怎么讲|当时|原原本本|哪一句|逐条|列一下|都做了|分别|多少|几号")

DEPTH_LAYERS = {
    "summary": ["DL3"],
    "detail": ["DL3", "DL2"],
    "full": ["DL3", "DL2", "DL1"],
}


def keywords(q: str) -> set[str]:
    s = str(q or "").lower()
    out: set[str] = set()
    for w in re.split(r"[\s,，。！？!?、；;:：\"'（）()\[\]]+", s):
        if len(w) >= 2 and w not in STOP:
            out.add(w)
    zh = re.sub(r"[^\u4e00-\u9fa5]", "", s)
    for i in range(len(zh) - 1):
        bg = zh[i : i + 2]
        if bg not in STOP:
            out.add(bg)
    return out


def parse_time(q: str, now: int) -> dict | None:
    d = time.localtime(now / 1000)
    start_today = int(time.mktime((d.tm_year, d.tm_mon, d.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)
    today = start_today
    table = {
        "刚刚": (now - 2 * 3600 * 1000, now),
        "今天": (today, today + DAY),
        "昨天": (today - DAY, today),
        "前天": (today - 2 * DAY, today - DAY),
        "这周": (today - 6 * DAY, today + DAY),
        "上周": (today - 13 * DAY, today - 6 * DAY),
        "最近": (today - 7 * DAY, today + DAY),
        "之前": (0, now),
    }
    for k, v in table.items():
        if k in str(q):
            return {"from": v[0], "to": v[1], "word": k}
    return None


def layer_boost(layer: str) -> float:
    return 1.0 if layer == "DL3" else (0.8 if layer == "DL2" else 0.6)


class Tiers:
    """一个数据目录的读写端。dir 相同 = 共享同一份记忆。"""

    def __init__(
        self,
        dir_path: str,
        writer: str = "astrbot",
        hot_max_chars: int = 3000,
        hot_quota_own: float = 0.7,
        long_max_chars: int = 3000,
        consolidate_max_chars: int = 2500,
        layer_cache_max_items: int = 300_000,
        rerank_pool: int = 30,
    ) -> None:
        self.dir = os.path.abspath(os.path.expanduser(str(dir_path)))
        self.writer = sanitize_writer(writer)
        self.hot_max_chars = int(hot_max_chars) if int(hot_max_chars) > 0 else 3000
        self.hot_quota_own = float(hot_quota_own) if 0 < float(hot_quota_own) <= 1 else 0.7
        # 阶段 B：L3「一直记得的」/ L2「最近沉淀的」的注入容量，和 dsh 同名配置对齐
        self.long_max_chars = int(long_max_chars) if int(long_max_chars) > 0 else 3000
        self.consolidate_max_chars = (
            int(consolidate_max_chars) if int(consolidate_max_chars) > 0 else 2500
        )
        self.tmp_dir = os.path.join(self.dir, "L1.tmp")
        # 只读层缓存：name -> (sig, items[, id索引])。key 带 mtime+size，
        # 对方（dsh）原子替换后自动失效；单层超过上限就不缓存，避免百万条吃满内存。
        self.layer_cache_max_items = max(0, int(layer_cache_max_items))
        # 送进精排的候选数（dsh 的 rerankPool 默认 30）
        self.rerank_pool = max(1, int(rerank_pool))
        self._cache: dict[str, tuple] = {}
        # 排序结果缓存：L2 每轮都要按 consolidatedAt 排一次，10 万条约 80ms
        self._sort_cache: dict[str, tuple] = {}
        self.cache_hits = 0
        self.cache_misses = 0

    # ---------- 路径 ----------

    def layer_path(self, name: str) -> str:
        return os.path.join(self.dir, f"{name}.json")

    @property
    def temp_path(self) -> str:
        return os.path.join(self.tmp_dir, f"{self.writer}.jsonl")

    def ensure_dirs(self) -> None:
        os.makedirs(self.tmp_dir, exist_ok=True)

    # ---------- 写：只追加自己的 temp ----------

    def make_item(
        self,
        text: str,
        speaker: str = "",
        channel: str = "dialog",
        scene: str = "",
        people: list[str] | None = None,
        importance: float = 0.5,
    ) -> dict:
        now = _now_ms()
        folks = []
        sp = str(speaker or "").strip()
        # speaker 是角色占位（user/bot）时不要混进 people——people 只放真名字
        extra = [sp] if sp and sp not in ("user", "bot") else []
        for x in list(people or []) + extra:
            s = str(x or "").strip()
            if s and s not in folks:
                folks.append(s)
        return {
            "id": new_id(),
            "seq": 0,  # 写盘前统一填
            "text": str(text or "").strip(),
            "kind": "raw",
            "channel": channel or "dialog",
            "layer": "L1",
            "speaker": speaker or "",
            "people": folks,
            "scene": scene or "",
            "importance": float(importance) if importance is not None else 0.5,
            "directEntry": False,
            "heat": 1,
            "lastMentionedAt": now,
            "repeatStrength": 1,
            "fusionCount": 0,
            "recallCount": 0,
            "supersededBy": None,
            "createdAt": now,
            "archivedAt": None,
            "spanFrom": None,
            "spanTo": None,
            "pinned": False,
            "archiveFile": None,
        }

    def write_turn(self, rows: list[dict], turn_id: str | None = None) -> dict:
        """把一轮（用户 + 助手）追加进自己的 temp。返回 {ok, turnId, ids, error}。"""
        global _seq
        tid = str(turn_id or "").strip() or new_turn_id(self.writer)
        items = []
        for r in rows or []:
            text = str((r or {}).get("text") or "").strip()
            if not text:
                continue
            # 没显式给 importance：先按 0.5 落库，打 impRough 等整理时问模型。
            # 写入路径上一次模型往返就是一两秒延迟，而刚写进来的条目可能
            # 马上被合并/作废，那时候分类纯属浪费——所以分类挪到整理时整批做。
            raw_imp = r.get("importance")
            rough = raw_imp is None
            it = self.make_item(
                text=text,
                speaker=r.get("speaker", ""),
                channel=r.get("channel", "dialog"),
                scene=r.get("scene", ""),
                people=r.get("people"),
                importance=0.5 if rough else float(raw_imp),
            )
            if rough:
                it["impRough"] = True
            # 允许调用方指定"这条是什么时候发生的"（补录历史、导入旧库时必需）。
            # 不指定才用写入时刻——真实实时场景两者本来相等。
            at = r.get("createdAt")
            if at is not None:
                try:
                    at = int(at)
                except (TypeError, ValueError):
                    at = None
            if at and at > 0:
                it["createdAt"] = at
                it["lastMentionedAt"] = int(r.get("lastMentionedAt") or at)
            it["turnId"] = tid
            it["origin"] = self.writer
            items.append(it)
        if not items:
            return {"ok": False, "turnId": tid, "ids": [], "error": "内容为空"}

        with _seq_lock:
            for it in items:
                _seq += 1
                it["seq"] = _seq

        payload = "\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n"
        try:
            self.ensure_dirs()
            # 追加，不重写；写完立即关闭——Windows 上打开的文件没法被 rename（陷阱 1）
            with open(self.temp_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(payload)
        except OSError as e:
            return {"ok": False, "turnId": tid, "ids": [], "error": str(e)}
        return {"ok": True, "turnId": tid, "ids": [it["id"] for it in items]}

    # ---------- 读：L1.json + 所有 temp ----------

    def read_layer(self, name: str) -> list[dict]:
        """读一层。文件不存在当空；损坏则抛（调用方决定降级）。"""
        try:
            with open(self.layer_path(name), "r", encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            return []
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            raise
        items = j.get("items") if isinstance(j, dict) else None
        return items if isinstance(items, list) else []

    def read_jsonl(self, path: str) -> tuple[list[dict], int]:
        """逐行解析；半行（对方正在追加）跳过那一行，不要整份放弃。"""
        items: list[dict] = []
        bad = 0
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        bad += 1
                        continue
                    if isinstance(obj, dict) and obj.get("id"):
                        items.append(obj)
        except OSError:
            return [], 0
        return items, bad

    def read_temps(self) -> tuple[list[dict], int]:
        items: list[dict] = []
        bad = 0
        try:
            names = sorted(os.listdir(self.tmp_dir))
        except OSError:
            return [], 0
        for n in names:
            if not n.endswith(".jsonl"):
                continue
            # *.claimed-* 是对方正在合并的（陷阱 8）：不读、不删
            if ".claimed-" in n:
                continue
            got, b = self.read_jsonl(os.path.join(self.tmp_dir, n))
            items.extend(got)
            bad += b
        return items, bad

    def read_l1_view(self) -> tuple[list[dict], int]:
        """L1 视图 = 真 L1 + 所有 temp，按 id 去重（顺序：真 L1 在前）。"""
        try:
            base = self._read_cached("L1")
        except (json.JSONDecodeError, OSError):
            base = []  # 对方正在原子替换的瞬间可能读到空/坏，下一轮再来
        temps, bad = self.read_temps()
        seen: set[str] = set()
        out: list[dict] = []
        for it in list(base) + temps:
            if not isinstance(it, dict):
                continue
            i = it.get("id")
            if not i or i in seen:
                continue
            seen.add(i)
            out.append(it)
        return out, bad

    # ---------- 注入 ----------

    def invalidate(self, name: str | None = None) -> None:
        """主动失效（正常靠 mtime 也能失效，这里只是兜底）。"""
        if name is None:
            self._cache.clear()
            self._sort_cache.clear()
        else:
            self._cache.pop(name, None)
            self._sort_cache.pop(name, None)

    def _read_cached(self, name: str) -> list[dict]:
        path = self.layer_path(name)
        try:
            st = os.stat(path)
        except OSError:
            self._cache.pop(name, None)
            return []
        sig = (st.st_mtime_ns, st.st_size)
        ent = self._cache.get(name)
        if ent is not None and ent[0] == sig:
            self.cache_hits += 1
            return ent[1]
        items = self.read_layer(name)
        self.cache_misses += 1
        if self.layer_cache_max_items and len(items) <= self.layer_cache_max_items:
            self._cache[name] = (sig, items)
        else:
            self._cache.pop(name, None)
        return items

    def _index_of(self, name: str) -> dict[str, dict]:
        """层内 id → item 索引（懒建，跟缓存条目一起失效）。没有缓存时返回空。"""
        ent = self._cache.get(name)
        if ent is None:
            return {}
        if len(ent) < 3:
            idx: dict[str, dict] = {}
            for it in ent[1]:
                i = it.get("id")
                if i:
                    idx[i] = it
            ent = (ent[0], ent[1], idx)
            self._cache[name] = ent
        return ent[2]

    def read_layer_safe(self, name: str) -> list[dict]:
        """读一层（带进程内缓存）：不存在当空；读到坏 JSON 也当空。

        注入绝不能因为别人的文件坏了一条就整块丢掉。

        ★ 只有只读路径（注入 / 检索 / 统计）走这里。整理必须 load_db 直读：
          缓存返回的是共享 list，整理会就地改对象，改到缓存就等于把下一次注入
          污染成"整理中的中间状态"。
        """
        try:
            return self._read_cached(name)
        except (json.JSONDecodeError, OSError):
            return []

    # ---------- L3 快照（照 memory6 的 readL3Snapshot / computeL3） ----------

    # 快照过期时现算的上限：小库现算只要几毫秒；大库读一次要几秒，
    # 注入是每轮都跑的，宁可看旧快照，等整理者下一轮刷新。
    L3_INLINE_MAX = 1.5 * 1024 * 1024

    def dl3_version(self) -> str:
        try:
            st = os.stat(self.layer_path("DL3"))
            return f"{int(st.st_mtime * 1000)}:{st.st_size}"
        except OSError:
            return "0:0"

    def dl3_bytes(self) -> int:
        try:
            return os.stat(self.layer_path("DL3")).st_size
        except OSError:
            return 0

    def compute_l3(self, now: int | None = None) -> list[dict]:
        """从 DL3 按 importance×heat×(融合/关注加成) 截前 N 条（纯计算，不写盘）。"""
        now = int(now if now is not None else time.time() * 1000)
        pool = self.read_layer_safe("DL3")
        scored = [
            (it, l3_score(it, now))
            for it in pool
            if not it.get("supersededBy")
        ]
        scored.sort(key=lambda x: -x[1])
        out: list[dict] = []
        used = 0
        for it, _s in scored:
            ln = len(str(it.get("text") or ""))
            if used + ln > self.long_max_chars:
                break
            out.append(it)
            used += ln
        return out

    def read_l3_snapshot(self) -> list[dict] | None:
        """只有「快照记的 DL3 版本 == 当前 DL3 版本」才算新鲜。

        过期时：小库返回 None（调用方现算，保证"对方刚写的东西下一轮就能看见"），
        大库直接给旧快照（读一次几十万条要一两秒，注入扛不住）。
        """
        try:
            with open(self.layer_path("L3"), "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        items = snap.get("items") if isinstance(snap, dict) else None
        items = items if isinstance(items, list) else []
        if isinstance(snap, dict) and isinstance(snap.get("dl3v"), str) and snap["dl3v"] == self.dl3_version():
            return items
        if self.dl3_bytes() <= self.L3_INLINE_MAX:
            return None
        return items

    def _sorted_l2(self, raw: list[dict]) -> list[dict]:
        """L2 按沉淀时间升序（老的在前），排序结果跟缓存签名一起记。

        10 万条排序约 80ms，而注入是每条消息都跑；文件没变就直接用上次的结果。
        """
        ent = self._cache.get("L2")
        sig = ent[0] if ent is not None else None
        if sig is not None:
            hit = self._sort_cache.get("L2")
            if hit is not None and hit[0] == sig:
                return hit[1]
        out = [it for it in raw if not it.get("supersededBy")]
        out.sort(
            key=lambda it: (
                int(it.get("consolidatedAt") or it.get("createdAt") or 0),
                str(it.get("id") or ""),
            )
        )
        if sig is not None:
            self._sort_cache["L2"] = (sig, out)
        return out

    def inspect_block(self) -> dict:
        """把注入块拆成分段结构，供 `/dsm ctx` 查看（不重复 build_block 的逻辑）。

        返回：
          total_chars —— 整块字数（含外壳）
          sections    —— [{name, count, chars, lines}]，按注入顺序
          hidden_l1   —— 即时层因容量没显示出来的条数（没这句就是 0）
        """
        block = self.build_block()
        sections: list[dict] = []
        hidden = 0
        cur: dict | None = None
        for ln in block.split("\n"):
            if ln.startswith("## "):
                cur = {"name": ln[3:].strip(), "lines": []}
                sections.append(cur)
                continue
            if ln.startswith("（即时层还有"):
                m = re.search(r"还有\s*(\d+)\s*条", ln)
                if m:
                    hidden = int(m.group(1))
                continue
            if cur is None or not ln.strip():
                continue
            if ln.startswith(("<dsh_memory", "</dsh_memory", "下面是从长期记忆")):
                continue
            cur["lines"].append(ln)
        for s in sections:
            s["count"] = len(s["lines"])
            s["chars"] = sum(len(x) + 1 for x in s["lines"])
        return {"total_chars": len(block), "sections": sections, "hidden_l1": hidden, "block": block}

    def build_block(self) -> str:
        """拼注入块，顺序与 dsh 的 buildBlock 一致：

        一直记得的（L3 摘要） → 最近沉淀的（L2） → 刚发生的（L1 视图）
        """
        # L3：快照新鲜就用快照，过期（对方刚整理完）就现算一份，不写盘
        l3 = self.read_l3_snapshot()
        if l3 is None:
            l3 = self.compute_l3()
        l3 = [it for it in l3 if not it.get("supersededBy")]
        # L2：刚沉淀下来的原话
        l2raw = self.read_layer_safe("L2")
        # 已经沉到 L2 的条目，不要再当"刚发生的"重复显示一遍
        placed = {str(it.get("id")) for it in l2raw if it.get("id")}

        items, _bad = self.read_l1_view()
        items = [
            it
            for it in items
            if not it.get("supersededBy") and str(it.get("id")) not in placed
        ]

        # L2 按沉淀时间升序（老的在前），排序结果带缓存
        l2 = self._sorted_l2(l2raw)
        # L1 新的在前；同毫秒按 id 倒序，保证两端顺序稳定
        items.sort(key=lambda it: str(it.get("id") or ""), reverse=True)
        items.sort(key=lambda it: int(it.get("createdAt") or 0), reverse=True)

        cap = self.hot_max_chars
        own_limit = max(1, int(cap * self.hot_quota_own))
        mine = _pack([it for it in items if it.get("origin") == self.writer], own_limit)
        used = sum(len(x) + 1 for x in mine)
        other = _pack(
            [it for it in items if it.get("origin") != self.writer],
            max(0, cap - used),
        )

        parts: list[str] = []
        # L3 在 compute_l3 里已经按 long_max_chars 截过条，这里不能再按字数包一遍：
        # 每条还要多算 1 个换行，会导致末尾少显示一条（dsh 用 pack(l3, Infinity)）。
        long_lines = _pack(l3, float("inf"))
        if long_lines:
            parts.append("## 一直记得的")
            parts.extend(long_lines)
        cons_lines = _pack(l2, self.consolidate_max_chars)
        if cons_lines:
            parts.append("## 最近沉淀的")
            parts.extend(cons_lines)
        if mine or other:
            parts.append("## 刚发生的")
            parts.extend(mine)
            parts.extend(other)

        shown = len(mine) + len(other)
        if shown < len(items):
            parts.append(f"（即时层还有 {len(items) - shown} 条未显示）")
        return wrap_block("\n".join(parts))

    # ---------- 检索（照 memory6 的 search） ----------

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        # 维度不一致直接判 0（与 dsh 的 cosine 一致）：截断后照算会给出"假相关性"，
        # 换过嵌入模型时这种分数比没有分数更危险。
        if not a or not b or len(a) != len(b):
            return 0.0
        n = len(a)
        dot = na = nb = 0.0
        for i in range(n):
            dot += a[i] * b[i]
            na += a[i] * a[i]
            nb += b[i] * b[i]
        if na <= 0 or nb <= 0:
            return 0.0
        return dot / (na ** 0.5) / (nb ** 0.5)

    async def search(
        self,
        query: str,
        *,
        depth: str = "auto",
        limit: int = 6,
        scene: str | None = None,
        vectors: dict[str, list[float]] | None = None,
        embed_one=None,
        rerank=None,
        use_rerank: bool = True,
        min_rerank: float = 0.02,
        follow_sources: bool = True,
        now: int | None = None,
    ) -> dict:
        """混合检索：关键词 + 向量 + 时间窗 + 分层加权，可选精排。

        返回 {hits, confidence, advice, depth, reranked, stages}，和 dsh 同结构。
        """
        import math

        q = str(query or "").strip()
        now = int(now if now is not None else time.time() * 1000)
        if depth not in DEPTH_LAYERS:
            depth = "detail" if DETAIL_HINTS.search(q) else "summary"

        win = parse_time(q, now)
        kw = keywords(q.replace(win["word"], "") if win else q)
        qvec = None
        if embed_one is not None:
            try:
                qvec = await embed_one(q)
            except Exception:
                qvec = None

        layers = DEPTH_LAYERS[depth]
        pool: list[dict] = []
        for l in layers:
            pool.extend(self.read_layer_safe(l))

        scored = []
        for idx, it in enumerate(pool):
            if it.get("supersededBy") and not re.search("以前|之前", q):
                continue
            if scene and it.get("scene") != scene:
                continue
            frm = int(it.get("spanFrom") or it.get("createdAt") or 0)
            to = int(it.get("spanTo") or it.get("createdAt") or 0)
            if win and not (to >= win["from"] and frm <= win["to"]):
                continue

            # 关键词命中改成逐字段短路：先只查 text，没命中的关键词才去拼
            # speaker/people/scene。99 万条里 94% 最终被丢掉，原实现给每一条
            # 都拼过一次 body，纯属白花。
            text = str(it.get("text") or "")
            hit = 0
            miss = []
            for k in kw:
                if k in text:
                    hit += 1
                else:
                    miss.append(k)
            if miss:
                rest = " ".join(
                    [
                        str(it.get("speaker") or ""),
                        " ".join(str(x) for x in (it.get("people") or [])),
                        str(it.get("scene") or ""),
                    ]
                )
                hit += sum(1 for k in miss if k in rest)
            coverage = (hit / len(kw)) if kw else 0.0
            v = (vectors or {}).get(it.get("id"))
            sem = self._cosine(qvec, v) if (qvec and v) else 0.0
            if kw and hit == 0 and sem < 0.2 and not win:
                continue

            time_score = 0.9 if win else 0.5
            imp = float(it.get("importance") or 0)
            boost = layer_boost(str(it.get("layer") or ""))
            if qvec:
                score = 0.42 * sem + 0.22 * coverage + 0.20 * time_score + 0.10 * imp + 0.06 * boost
            else:
                score = 0.55 * coverage + 0.25 * time_score + 0.12 * imp + 0.08 * boost
            scored.append({
                "it": it, "score": score, "sem": sem,
                "coverage": coverage, "rel": None, "_i": idx,
            })

        # 精排池：dsh 用 rerankPool=30。只取 limit*2 的话，相关条目经常在粗排
        # 阶段就掉出池子，精排等于没用（实测 bge-reranker 给相关文档 0.95 分，
        # 但那条根本没被送进去）。
        pool_n = max(limit, min(len(scored), max(limit * 2, self.rerank_pool)))
        if len(scored) > pool_n:
            # 只取前 pool_n，不做全量排序。(score 降序, 原序) 与稳定排序逐条一致
            top = heapq.nsmallest(pool_n, scored, key=lambda x: (-x["score"], x["_i"]))
        else:
            scored.sort(key=lambda x: -x["score"])
            top = scored

        reranked = False
        if use_rerank and rerank is not None and len(top) > 1:
            try:
                rs = await rerank(q, [str(x["it"].get("text") or "") for x in top])
                if isinstance(rs, list) and rs:
                    rel: dict[int, float] = {}
                    for i, x in enumerate(rs):
                        if isinstance(x, dict):
                            # 硅基流动的 /rerank 返回的是 relevance_score，不是 score；
                            # dsh 的 silicon 会把它们统一成 score，这里两种都认。
                            sc = x.get("score")
                            if sc is None:
                                sc = x.get("relevance_score")
                            rel[int(x.get("index") or 0)] = float(sc or 0)
                        else:
                            rel[i] = float(x or 0)
                    ranked = []
                    for i, x in enumerate(top):
                        r = rel.get(i, 0.0)
                        if r < min_rerank:
                            continue
                        ranked.append({**x, "rel": r, "score": 0.55 * r + 0.45 * x["score"]})
                    ranked.sort(key=lambda x: -x["score"])
                    if ranked:  # 全被滤光说明精排降级了，保留原分
                        top = ranked
                        reranked = True
            except Exception:
                pass

        def _row(x: dict) -> dict:
            it = x["it"]
            return {
                "id": it.get("id"),
                "text": it.get("text"),
                "layer": it.get("layer"),
                "channel": it.get("channel"),
                "when": _day_key(it.get("createdAt")),
                "scene": it.get("scene") or "",
                "createdAt": it.get("createdAt"),
                "spanFrom": it.get("spanFrom"),
                "importance": it.get("importance"),
                "score": round(x["score"], 4),
                # 纯语义相似度：语义注入按它卡门槛（score 是混合分，不能当余弦用）
                "sem": round(float(x.get("sem") or 0), 4),
                "rel": x.get("rel"),
                "kind": it.get("kind"),
                "sources": it.get("sources") or [],
            }

        hits = [_row(x) for x in top[:limit]]

        # detail/full：顺着摘要的 sources 去 DL2/DL1 取原文（比拿查询扫 DL2 准）
        if depth != "summary" and follow_sources:
            # 两层先入缓存（命中几乎免费），再按 id 查懒建索引。
            # 不再每次检索都把 DL2/DL1 重建成一个大 dict：99 万条时那是 20 万次 set。
            for l in ("DL2", "DL1"):
                self.read_layer_safe(l)
            deep_fallback: dict[str, dict] | None = None

            def find_deep(sid: str):
                nonlocal deep_fallback
                hit = self._index_of("DL2").get(sid) or self._index_of("DL1").get(sid)
                if hit is not None:
                    return hit
                # 层太大没进缓存：兜底建一次 map（只在这一种情况下慢）
                if deep_fallback is None:
                    deep_fallback = {}
                    for l in ("DL2", "DL1"):
                        for x in self.read_layer_safe(l):
                            deep_fallback.setdefault(x.get("id"), x)
                return deep_fallback.get(sid)

            seen = {h["id"] for h in hits}
            extra = []
            for h in hits:
                if h.get("kind") != "summary" or not h.get("sources"):
                    continue
                for sid in h["sources"]:
                    if sid in seen:
                        continue
                    it = find_deep(sid)
                    if not it:
                        continue
                    seen.add(sid)
                    extra.append(
                        {
                            "id": it.get("id"),
                            "text": it.get("text"),
                            "layer": it.get("layer"),
                            "channel": it.get("channel"),
                            "when": _day_key(it.get("createdAt")),
                            "scene": it.get("scene") or "",
                            "createdAt": it.get("createdAt"),
                            "spanFrom": it.get("spanFrom"),
                            "importance": it.get("importance"),
                            "score": round(h["score"] * 0.9, 4),
                            "sem": 0.0,
                            "rel": h.get("rel"),
                            "kind": it.get("kind"),
                            "via": h["id"],
                        }
                    )
            hits.extend(extra[:6])

        rel_max = max([(h.get("rel") if h.get("rel") is not None else h.get("score") or 0) for h in hits] or [0])
        # 有精排时 rel 是 rerank 原始分（量级 0.0x~1.0）；没有精排时 rel_max 其实是综合分，
        # 两者不能用同一组阈值，否则「相关度」建议永远是同一句。
        high, mid = (0.5, 0.1) if reranked else (0.55, 0.38)
        if not hits:
            advice = "没有找到相关记忆。可以换 depth='full' 再搜一次，或确认这件事确实没记过。"
        elif rel_max >= high:
            advice = "相关度较高，可以直接用。"
        elif rel_max >= mid:
            advice = "相关度中等，用之前自己核一下是不是真的在说这件事。"
        else:
            advice = "相关度偏低（可能只是字面擦边）。如果跟当前话题无关，换 depth='full' 再搜一次。"
        return {
            "hits": hits,
            "confidence": round(rel_max, 3),
            "advice": advice,
            "depth": depth,
            "reranked": reranked,
            "stages": layers,
        }

    def stats(self) -> dict:
        items, bad = self.read_l1_view()
        by_origin: dict[str, int] = {}
        for it in items:
            o = str(it.get("origin") or "?")
            by_origin[o] = by_origin.get(o, 0) + 1
        layers = {
            name: len(self.read_layer_safe(name))
            for name in ("L1", "L2", "DL1", "DL2", "DL3")
        }
        return {
            "dir": self.dir,
            "writer": self.writer,
            "temp": self.temp_path,
            "l1": len(items),
            "l2": layers["L2"],
            "l3": len(self.read_layer_safe("L3")),
            "layers": layers,
            "by_origin": by_origin,
            "bad_lines": bad,
            "temp_exists": os.path.exists(self.temp_path),
            "cache": {"hits": self.cache_hits, "misses": self.cache_misses},
        }
