"""阶段 C：astrbot 自己拿钥匙整理（dsh 不在线时也能沉淀）。

结构照抄 dsh 的 `memory6.mjs`：

    organize()
      ├─ acquireLock()          open('organize.lock','x') 创建成功才算拿到
      ├─ reconcile()            跨层去重：同一 id 只能在一层，DL2 不留孤儿
      ├─ mergeTemps()           认领所有端的 *.jsonl（rename 成功才算收下）→ 并进 L1
      ├─ organizeL1()           L1 超容量 → 按轮取安静了的旧一半 → digest(第一次)
      ├─ organizeL2()           L2 超容量 → 取旧一半 + DL1 全部 → digest(第二次)
      ├─ forgetDL1()            DL1 保持率低于阈值 → 写台账再删
      ├─ saveDb()               先写 L2/DL1/DL2/DL3，最后写 L1
      └─ refreshL3()            从 DL3 按 importance×heat 截前 N 条写快照

两条保证：
1. **写盘顺序**：崩在中间时 L1 里还有原文，靠 id 去重不会丢也不会重。
2. **无模型不装作整理**：没有摘要回调时整轮跳过；有回调但失败时整批回 L2（下轮再压）。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import shutil
import threading
import time
from typing import Any, Awaitable, Callable

try:  # 作为包导入（astrbot 正常路径）
    from .tiers import DAY, effective_strength, half_life_of, heat_of, retention_of
    from .vectors import cosine
except ImportError:  # 直接跑单测（sys.path 里有插件目录）
    from tiers import DAY, effective_strength, half_life_of, heat_of, retention_of  # type: ignore
    from vectors import cosine  # type: ignore

# 与 memory5/memory6 的默认值对齐（改了要两端一起改）
ORG_DEFAULTS = {
    "hot_max_chars": 3000,
    "hot_max_items": 200,
    # 只整理"安静了这么久"的轮次，避免聊到一半被腰斩
    "quiet_ms": 10 * 60 * 1000,
    "consolidate_max_chars": 2500,
    "consolidate_max_items": 120,
    "l2_keep_max": 10,
    # 摘要单次最多喂多少条（和 dsh 的 summaryChunk 一致）：超过就切片，逐片调用
    "summary_chunk": 60,
    # 嵌入分批：硅基流动单请求有上限，整组一次传必被拒；失败只丢这一批
    "embed_chunk": 32,
    "embed_concurrency": 4,
    # 跨批判重：向量预筛阈值（和 memory5/6 一致，略收紧减少无意义的模型调用）
    "fusion_prefilter": 0.82,
    "long_max_chars": 3000,
    "view_refresh_ms": 30 * 60 * 1000,
    "forget_threshold": 0.25,
    "stale_lock_ms": 10 * 60 * 1000,
    "max_organize_ms": 5 * 60 * 1000,
    # 单层超过这么多条就在 audit 里告警（不改行为，只方便排查）
    "warn_layer_items": 200_000,
    # 热度（tune.mjs 扫出来的，别单独调）
    "heat_half_life": 10.0,
    "heat_repeat_boost": 0.3,
    "heat_repeat_cap": 20,
}

DATA_LAYERS = ("L1", "L2", "DL1", "DL2", "DL3")
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def write_signal(
    dir_path: str,
    writer: str,
    op: str,
    ids: list[str],
    v: bool | None = None,
) -> dict:
    """提交一条信号（attend / pin / forget），由下一次整理消费。

    只追加自己的 `<dir>/signals/<writer>.jsonl`，字段与 dsh 的 `signal()` 完全一致：
    `{opId, op, ids, at[, v]}`。opId 让整理侧能防重放（applied.json）。
    """
    now = _now()
    rec: dict = {
        "opId": f"{writer}-{_b36(now)}-{random.randrange(1 << 16):04x}",
        "op": str(op),
        "ids": [str(x).strip() for x in ids if str(x).strip()],
        "at": now,
    }
    if v is not None:
        rec["v"] = bool(v)
    sig_dir = os.path.join(dir_path, "signals")
    os.makedirs(sig_dir, exist_ok=True)
    with open(os.path.join(sig_dir, f"{writer}.jsonl"), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _b36(n: int) -> str:
    n = int(n)
    if n <= 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return "".join(reversed(out))


def _now() -> int:
    return int(time.time() * 1000)


def _chars(items) -> int:
    return sum(len(str(it.get("text") or "")) for it in items)


def raw_line(it: dict) -> str:
    """送模型的一行原文：``[MM-DD] 说话人：正文``（与 dsh 的 `rawLine` 逐字一致）。

    为什么带日期：模型看不到条目的 createdAt，原文里的「明天/昨天/下周三」
    就没有锚点，摘要只能丢掉或原样照抄，读者过几天再看就不知道是哪天。
    带上这条发生的日期，模型才能把相对时间折算成具体日期。

    用 MM-DD 而不是完整时间戳：与注入块 `line_of` 的日期口径一致，
    且同一批通常跨不了几年；原文自带年份时模型会照抄。
    """
    ts = int(it.get("createdAt") or 0)
    stamp = ""
    if ts > 0:
        d = time.localtime(ts / 1000)
        stamp = f"[{d.tm_mon:02d}-{d.tm_mday:02d}] "
    sp = str(it.get("speaker") or "").strip()
    body = str(it.get("text") or "")
    return f"{stamp}{sp}：{body}" if sp else f"{stamp}{body}"


def _uniq(seq) -> list:
    out = []
    seen = set()
    for x in seq:
        if x in (None, ""):
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _cmp_id(a, b) -> int:
    return -1 if str(a) < str(b) else (1 if str(a) > str(b) else 0)


def src_key(items) -> str:
    """来源集合的指纹：同一批原文只出一次摘要（照 dsh 的 5381 哈希）。"""
    s = ",".join(sorted(str(it.get("id")) for it in items))
    h = 5381
    for ch in s:
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    return "k" + _b36(h)


def _day_of(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts) / 1000))
    except (TypeError, ValueError, OSError):
        return ""


def spacing_quality(ratio: float) -> float:
    """间隔重复效应：隔几天比隔一天强化得多（memory5 同一个公式）。"""
    return min(1.0, 0.15 + 0.85 * math.log2(1 + max(0.0, float(ratio))))


def touch_heat(it: dict, now: int, cfg: dict) -> float:
    """记录一次"被提及"。同一天多次只算一次，否则爆发会压过持续。"""
    last = it.get("lastMentionedAt")
    if last is None:
        last = it.get("createdAt")
    if last is None:
        last = now
    if _day_of(now) == _day_of(last):
        it["lastMentionedAt"] = max(int(last), int(now))
        return float(it.get("heat") or 0)

    dt = (now - float(last)) / DAY
    half = half_life_of(it, cfg, int(last))
    ratio = dt / half
    q = spacing_quality(ratio)
    it["heat"] = float(it.get("heat") or 0) * (2.0 ** (-ratio)) + q
    # 先让旧的强化按遗忘曲线回落，再加这次的分——否则回落永远被抹掉
    it["repeatStrength"] = effective_strength(it, int(last), cfg) + q
    it["lastMentionedAt"] = now
    return float(it["heat"])


def pick_turn_batch(items, cap, *, order_key, quiet_before=0, max_items=None):
    """按轮次取旧的一半：从旧到新整轮拿，直到剩下的字数 ≤ cap/2。

    一轮特别大时会把整层取空，这是允许的（dsh 的 pickTurnBatch 同规则）。
    """
    if not items:
        return []
    total = _chars(items)
    if total <= cap and (max_items is None or len(items) <= max_items):
        return []
    groups: dict[str, list] = {}
    for it in items:
        k = str(it.get("turnId") or it.get("id"))
        groups.setdefault(k, []).append(it)
    turns = [
        {"items": g, "t": min(float(order_key(x) or 0) for x in g)}
        for g in groups.values()
    ]
    turns.sort(key=lambda x: x["t"])

    pinned_turns = [t for t in turns if any(it.get("pinned") for it in t["items"])]
    pinned_chars = sum(_chars(t["items"]) for t in pinned_turns)
    pinned_items = sum(len(t["items"]) for t in pinned_turns)
    remaining = total - pinned_chars
    remaining_items = len(items) - pinned_items
    char_target = cap / 2
    item_target = (math.ceil(max_items / 2)) if max_items else float("inf")

    out = []
    for turn in turns:
        if remaining <= char_target and remaining_items <= item_target:
            break
        if any(it.get("pinned") for it in turn["items"]):
            continue
        if quiet_before and max(float(it.get("createdAt") or 0) for it in turn["items"]) > quiet_before:
            break
        out.extend(turn["items"])
        remaining -= _chars(turn["items"])
        remaining_items -= len(turn["items"])
    return out


def parse_index_list(raw: Any, n: int) -> list[int]:
    """解析《1,3,5》《1-3》这类出处编号（1 起）。"""
    out = set()
    for part in re.split(r"[,，、\s]+", str(raw or "")):
        if not part:
            continue
        rng = re.match(r"^(\d+)\s*[-—~～]\s*(\d+)$", part)
        if rng:
            a, b = int(rng.group(1)), int(rng.group(2))
            if a >= 1 and b >= a and b <= n:
                for i in range(a, b + 1):
                    out.add(i)
            continue
        if part.isdigit():
            k = int(part)
            if 1 <= k <= n:
                out.add(k)
    return sorted(out)


def parse_summary_reply(reply: str, n: int) -> dict | None:
    """解析摘要回复：★/· 摘要行 + 《出处》，`+ 3,7` 这类保留行。

    返回 {"summaries":[{text, importance, sources}], "keep":[int]}
    """
    if not isinstance(reply, str) or not reply.strip():
        return None
    sums = []
    keep: list[int] = []
    for raw in reply.split("\n"):
        line = raw.strip()
        if not line:
            continue
        plus = re.match(r"^[+＋]\s*([\d,，、\s\-—~～]+)$", line)
        if plus:
            keep.extend(parse_index_list(plus.group(1), n))
            continue
        imp = 0.9 if re.match(r"^[★☆]", line) else 0.7
        line = re.sub(r"^[★☆·•\-*＋+]\s*", "", line)
        if not line:
            continue
        m = re.match(r"^(.*?)[《<【(（]\s*([\d,，、\s\-—~～]+)\s*[》>】)）]\s*$", line)
        if not m:
            plain = re.sub(r"[《》]", "", line).strip()
            if plain:
                sums.append({"text": plain, "importance": imp, "sources": []})
            continue
        text = m.group(1).strip()
        if text:
            sums.append({"text": text, "importance": imp, "sources": parse_index_list(m.group(2), n)})
    if not sums and not keep:
        return None
    return {"summaries": sums, "keep": sorted(set(keep))}


class Organizer:
    """一个数据目录的整理端。和 dsh 抢同一把 organize.lock。"""

    def __init__(self, dir_path: str, writer: str = "astrbot", cfg: dict | None = None,
                 vector_store=None) -> None:
        self.dir = os.path.abspath(os.path.expanduser(str(dir_path)))
        self.writer = str(writer or "astrbot")
        self.cfg = {**ORG_DEFAULTS, **{k: v for k, v in (cfg or {}).items() if v is not None}}
        self.vectors = vector_store
        self.tmp_dir = os.path.join(self.dir, "L1.tmp")
        self.grave_dir = os.path.join(self.dir, "graveyard")
        self.lock_path = os.path.join(self.dir, "organize.lock")
        self.state_path = os.path.join(self.dir, "organize.state.json")
        self.audit_path = os.path.join(self.dir, "audit.log")
        self._lock_token = ""
        self._hb_stop: threading.Event | None = None
        # L3 快照：只有内容变了 / 到刷新间隔 / 快照过期 才重写
        self._l3_dirty = False
        self._last_l3_at = 0

    # ---------- 基础 IO ----------

    def path(self, layer: str) -> str:
        return os.path.join(self.dir, f"{layer}.json")

    def audit(self, line: str) -> None:
        try:
            with open(self.audit_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [astrbot] {line}\n")
        except OSError:
            pass

    def atomic_write(self, path: str, text: str) -> None:
        """tmp + replace。Windows 上目标被占用会短暂失败，重试几次。"""
        tmp = f"{path}.tmp-{os.getpid()}-{random.randrange(1 << 20)}"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        last = None
        for _ in range(6):
            try:
                os.replace(tmp, path)
                return
            except OSError as e:  # 正被对方读
                last = e
                time.sleep(0.03)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise OSError(f"写文件失败 {path}: {last}")

    def read_layer(self, layer: str) -> list[dict]:
        try:
            with open(self.path(layer), "r", encoding="utf-8") as f:
                j = json.load(f)
        except FileNotFoundError:
            return []
        items = j.get("items") if isinstance(j, dict) else None
        return items if isinstance(items, list) else []

    def write_layer(self, layer: str, items: list[dict]) -> None:
        self.atomic_write(
            self.path(layer),
            json.dumps({"version": 2, "at": _now(), "items": items}, ensure_ascii=False),
        )

    def read_layer_safe(self, name: str) -> list[dict]:
        try:
            return self.read_layer(name)
        except (json.JSONDecodeError, OSError):
            return []

    def load_db(self) -> dict:
        return {l: self.read_layer(l) for l in DATA_LAYERS}

    def sort_db(self, db: dict) -> None:
        db["L1"].sort(key=lambda it: (int(it.get("createdAt") or 0), str(it.get("id") or "")))
        db["L2"].sort(
            key=lambda it: (
                int(it.get("consolidatedAt") or it.get("createdAt") or 0),
                str(it.get("id") or ""),
            )
        )
        for l in ("DL1", "DL2", "DL3"):
            db[l].sort(key=lambda it: (int(it.get("createdAt") or 0), str(it.get("id") or "")))

    def save_db(self, db: dict, skip: tuple[str, ...] = ()) -> None:
        """**先写目标层，最后写 L1**（崩了也不丢原文）。"""
        self.sort_db(db)
        for l in ("L2", "DL1", "DL2", "DL3", "L1"):
            if l in skip and os.path.exists(self.path(l)):
                continue
            self.write_layer(l, db[l])

    # ---------- 锁 ----------

    def acquire_lock(self) -> bool:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                st = os.stat(self.lock_path)
            except OSError:
                return False
            if _now() - st.st_mtime * 1000 < float(self.cfg["stale_lock_ms"]):
                return False
            # 心跳过期：改名（原子）后再抢一次，别直接删别人的锁
            try:
                os.rename(self.lock_path, f"{self.lock_path}.stale-{_now()}")
            except OSError:
                return False
            return self.acquire_lock()
        except OSError as e:
            self.audit(f"LOCK_FAIL {e}")
            return False

        token = f"{self.writer}-{os.getpid()}-{_now()}-{random.randrange(1 << 20):x}"
        payload = json.dumps(
            {"pid": os.getpid(), "writer": self.writer, "at": _now(), "tab": token, "phase": "start"}
        )
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        self._lock_token = token
        self._start_heartbeat()
        return True

    def _start_heartbeat(self) -> None:
        stop = threading.Event()
        self._hb_stop = stop

        def beat() -> None:
            while not stop.wait(30):
                try:
                    os.utime(self.lock_path, None)
                except OSError:
                    return

        threading.Thread(target=beat, name="dsh-mem-hb", daemon=True).start()

    def check_lock(self) -> bool:
        """确认钥匙还是自己的（被接管就立刻停手）。"""
        if not self._lock_token:
            return False
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                cur = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False
        return cur.get("tab") == self._lock_token

    def release_lock(self) -> None:
        if self._hb_stop is not None:
            self._hb_stop.set()
            self._hb_stop = None
        if not self._lock_token:
            return
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                cur = json.load(f)
            if cur.get("tab") == self._lock_token:
                os.unlink(self.lock_path)
        except (OSError, json.JSONDecodeError):
            pass
        self._lock_token = ""

    # ---------- 认领 temp ----------

    def claim_temps(self) -> list[str]:
        """把当前所有 *.jsonl 改名认领。rename 失败 = 对方正在写，下一轮再试。"""
        claimed: list[str] = []
        try:
            names = sorted(os.listdir(self.tmp_dir))
        except OSError:
            return claimed
        now = _now()
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            src = os.path.join(self.tmp_dir, name)
            dst = os.path.join(
                self.tmp_dir,
                f"{name[:-6]}.claimed-{now}-{random.randrange(1 << 16):04x}.jsonl",
            )
            try:
                os.rename(src, dst)
                claimed.append(dst)
            except OSError:
                continue  # 被打开/正在写
        return claimed

    def merge_temps(self, db: dict) -> dict:
        claimed = self.claim_temps()
        if not claimed:
            return {"merged": 0, "claimed": [], "bad": 0}
        have = {it.get("id") for it in db["L1"]}
        for l in ("L2", "DL1", "DL2", "DL3"):
            for it in db[l]:
                have.add(it.get("id"))
        merged = 0
        bad = 0
        for p in claimed:
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.read().split("\n")
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    it = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if not isinstance(it, dict) or not it.get("id") or it["id"] in have:
                    continue
                if not it.get("origin"):
                    it["origin"] = "?" if str(it["id"]).startswith("m5_") else self.writer
                it.setdefault("turnId", it["id"])
                it["layer"] = "L1"
                have.add(it["id"])
                db["L1"].append(it)
                merged += 1
        if bad:
            self.audit(f"MERGE_TEMP_BAD {bad} 行（半行，已跳过）")
        return {"merged": merged, "claimed": claimed, "bad": bad}

    # ---------- 对账 ----------

    def reconcile(self, db: dict) -> int:
        removed = 0
        deep: dict[str, str] = {}
        for l in ("DL3", "DL2", "DL1"):
            before = len(db[l])
            db[l] = self._dedupe(db[l])
            removed += before - len(db[l])
            for it in db[l]:
                deep.setdefault(it.get("id"), l)

        l1_before = len(db["L1"])
        db["L1"] = [it for it in self._dedupe(db["L1"]) if it.get("id") not in deep]
        l2_before = len(db["L2"])
        db["L2"] = [it for it in self._dedupe(db["L2"]) if it.get("id") not in deep]
        in_l2 = {it.get("id") for it in db["L2"]}
        db["L1"] = [it for it in db["L1"] if it.get("id") not in in_l2]
        removed += (l1_before - len(db["L1"])) + (l2_before - len(db["L2"]))

        seen = set()
        for l in ("DL3", "DL2", "DL1"):
            kept = []
            for it in db[l]:
                if it.get("id") in seen:
                    removed += 1
                    continue
                seen.add(it.get("id"))
                kept.append(it)
            db[l] = kept

        # DL2 不变式：每一条都必须被某条 DL3 摘要引用，孤儿降到 DL1
        refs = set()
        for s in db["DL3"]:
            for i in s.get("sources") or []:
                refs.add(i)
        orphans = [it for it in db["DL2"] if it.get("id") not in refs]
        if orphans:
            db["DL2"] = [it for it in db["DL2"] if it.get("id") in refs]
            have = {it.get("id") for it in db["DL1"]}
            for it in orphans:
                if it.get("id") in have:
                    continue
                it["layer"] = "DL1"
                it.pop("consolidatedAt", None)
                db["DL1"].append(it)
            self.audit(f"RECONCILE_DL2_ORPHAN {len(orphans)} 条 → DL1")
            removed += len(orphans)
        return removed

    @staticmethod
    def _dedupe(items) -> list[dict]:
        seen = set()
        out = []
        for it in items:
            if not isinstance(it, dict) or not it.get("id") or it["id"] in seen:
                continue
            seen.add(it["id"])
            out.append(it)
        return out

    # ---------- 分流 ----------

    def _move(self, db: dict, it: dict, target: str, now: int) -> None:
        if it.get("layer") == target:
            if not any(x.get("id") == it.get("id") for x in db[target]):
                db[target].append(it)
            return
        for l in DATA_LAYERS:
            db[l] = [x for x in db[l] if x.get("id") != it.get("id")]
        it["layer"] = target
        if target == "L2":
            it["consolidatedAt"] = now
        db[target].append(it)

    def _warn_layers(self, db: dict) -> None:
        """单层过大时留一条审计：不改行为，只让"为什么这么慢"有据可查。"""
        warn = int(self.cfg.get("warn_layer_items") or 200_000)
        big = {l: len(db[l]) for l in DATA_LAYERS if len(db[l]) > warn}
        if big:
            self.audit("LAYER_WARN " + " ".join(f"{k}={v}" for k, v in big.items())
                       + f"（阈值 {warn}，建议检查整理是否卡住）")

    def _move_many(self, db: dict, items: list[dict], target: str, now: int) -> None:
        """批量搬层：一次扫层，而不是每条扫 5 层。

        语义与 `_move` 完全一致（同层幂等 / 搬走时从所有其它层摘掉 / L2 记
        consolidatedAt），只是把 O(N×层大小) 的一次一条变成 O(5×层大小 + N)。
        10 万条整批下沉从 35 分钟降到毫秒级。
        """
        if target not in DATA_LAYERS or not items:
            return
        ids = {it.get("id") for it in items if it.get("id")}
        if not ids:
            return
        for l in DATA_LAYERS:
            if l == target:
                continue
            db[l] = [x for x in db[l] if x.get("id") not in ids]
        in_target = {x.get("id") for x in db[target] if x.get("id")}
        for it in items:
            i = it.get("id")
            if i and i in in_target:
                continue                      # 同层幂等：已经在目标层里
            it["layer"] = target
            if target == "L2":
                it["consolidatedAt"] = now
            db[target].append(it)

    @staticmethod
    def _group_of(it: dict) -> str:
        return f"{it.get('origin') or '?'}\x01{it.get('scene') or ''}\x01{it.get('channel') or 'dialog'}"

    @staticmethod
    def _common_scene(items) -> str:
        scenes = _uniq(it.get("scene") for it in items)
        return scenes[0] if len(scenes) == 1 else ""

    # ---------- 摘要融合（照 memory6 的 mergeSummaries / fuseSummary） ----------

    def merge_summaries(self, target: dict, src: dict, now: int) -> None:
        """判为 duplicate：把新的并进旧的（文本取长的、时间窗取并集、热度取大）。"""
        if len(str(src.get("text") or "")) > len(str(target.get("text") or "")):
            target["text"] = src.get("text")
        t_from = target.get("spanFrom") or target.get("createdAt")
        t_to = target.get("spanTo") or target.get("createdAt")
        s_from = src.get("spanFrom") or src.get("createdAt")
        s_to = src.get("spanTo") or src.get("createdAt")
        target["spanFrom"] = min(t_from, s_from)
        target["spanTo"] = max(t_to, s_to)
        target["people"] = _uniq([*(target.get("people") or []), *(src.get("people") or [])])[:10]
        target["sources"] = _uniq([*(target.get("sources") or []), *(src.get("sources") or [])])
        target["importance"] = max(
            float(target.get("importance") or 0), float(src.get("importance") or 0)
        )
        target["heat"] = max(heat_of(target, now, self.cfg), heat_of(src, now, self.cfg))
        target["lastMentionedAt"] = now
        target["repeatStrength"] = max(
            float(target.get("repeatStrength") or 1), float(src.get("repeatStrength") or 1)
        )
        target["fusionCount"] = int(target.get("fusionCount") or 0) + 1
        target["lastFusedAt"] = now   # 衰减从这里起算

    async def fuse_summary(
        self,
        db: dict,
        sum_item: dict,
        now: int,
        judge_relation=None,
    ) -> tuple[str, dict | None]:
        """摘要入 DL3 前先找相似摘要，让模型判关系。

        duplicate → 并进旧的；contradiction → 新的取代旧的；update → 两条都留、记 updates。
        返回 (outcome, target)，outcome ∈ merged|superseded|updated|added，
        target 是发生关系的那条旧摘要（merged 时要重算它的向量）。
        """
        store = self.vectors
        sv = store.get(sum_item.get("id")) if store is not None else None
        if sv and judge_relation is not None:
            cands = []
            for it in db["DL3"]:
                if it.get("id") == sum_item.get("id"):
                    continue
                if it.get("kind") != "summary" or it.get("supersededBy"):
                    continue
                v = store.get(it.get("id")) if store is not None else None
                if not v:
                    continue
                sim = cosine(sv, v)
                if sim >= float(self.cfg["fusion_prefilter"]):
                    cands.append((it, sim))
            if cands:
                cands.sort(key=lambda x: -x[1])
                top = cands[0][0]
                try:
                    typ = await judge_relation(top.get("text"), sum_item.get("text"))
                except Exception as e:
                    # 判关系是模型活：失败就"不清洗、两条都留"，但必须留痕
                    self.audit(f"MODEL_FAIL relation {e}（未清洗，两条都留）")
                    typ = None
                if typ is True:
                    typ = "duplicate"
                if typ is False:
                    typ = None

                if typ == "duplicate":
                    self.merge_summaries(top, sum_item, now)
                    if store is not None:
                        store.forget(sum_item.get("id"))
                    self._l3_dirty = True
                    self.audit(f"FUSE_DUP {sum_item.get('id')} → {top.get('id')}")
                    return "merged", top

                if typ == "contradiction":
                    sum_item["supersedes"] = top.get("id")
                    top["supersededBy"] = sum_item.get("id")
                    sum_item["heat"] = max(
                        float(sum_item.get("heat") or 1), float(top.get("heat") or 1)
                    )
                    sum_item["repeatStrength"] = max(
                        float(sum_item.get("repeatStrength") or 1),
                        float(top.get("repeatStrength") or 1),
                    )
                    db["DL3"].append(sum_item)
                    self._l3_dirty = True
                    self.audit(f"SUPERSEDE {top.get('id')} ← {sum_item.get('id')}")
                    return "superseded", top

                if typ == "update":
                    # 进展：两条都留，只记"新的接在旧的后面"。
                    # ★ memory6 最初漏了这个分支，看起来像"融合完全没生效"。
                    sum_item["updates"] = top.get("id")
                    db["DL3"].append(sum_item)
                    self._l3_dirty = True
                    self.audit(f"FUSE_UPDATE {top.get('id')} → {sum_item.get('id')}（进展，两条都保留）")
                    return "updated", top

        db["DL3"].append(sum_item)
        self._l3_dirty = True
        return "added", None

    def _new_summary(self, text: str, channel: str, folks, scene: str, importance: float, now: int) -> dict:
        return {
            "id": f"m5_s{_b36(_now())[-5:]}{random.randrange(1 << 16):04x}",
            "seq": 0,
            "text": text,
            "kind": "summary",
            "channel": channel,
            "layer": "DL3",
            "speaker": "",
            "people": folks,
            "scene": scene,
            "importance": importance,
            "heat": 2,
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
            "origin": None,
        }

    async def digest(
        self,
        db: dict,
        batch: list[dict],
        summarize: Callable[..., Awaitable[Any]],
        *,
        second_chance: bool = False,
        embed_batch=None,
        judge_relation=None,
        started: int | None = None,
    ) -> int:
        """一批原文 → 摘要 + 路由。成功才搬；模型失败整批回 L2（不丢）。"""
        groups: dict[str, list] = {}
        for it in batch:
            groups.setdefault(self._group_of(it), []).append(it)
        now = _now()
        made = 0
        kept_total = 0

        for key, group in groups.items():
            # 每片前查一次超时：模型卡住时能退出并放锁，而不是一直占着钥匙
            if started is not None:
                self._check_timeout(started)
            origin, scene, channel = key.split("\x01")

            # 没有摘要能力：不产生假摘要，也不塞进 L2（L2 会进上下文）。
            # 整组下沉 DL1 并打 pendingDigest —— 不丢、不挤上下文、不参与遗忘，
            # 等有模型时 L2 满了会带上 DL1 全部一起压。
            if summarize is None:
                for it in group:
                    it["pendingDigest"] = True
                self._move_many(db, group, "DL1", now)
                self.audit(
                    f"DIGEST_NO_MODEL {origin}/{scene or '-'}/{channel} "
                    f"{len(group)} 条 → DL1（等有模型再整理）"
                )
                continue

            folks = _uniq(
                [p for it in group for p in (it.get("people") or ([it.get("speaker")] if it.get("speaker") else []))]
            )[:10]
            hint = ""
            if scene:
                hint = f"这一段来自场景「{scene}」，摘要不要和别的场景混在一起。"
            if second_chance:
                extra = f"这是第二次整理：+ 最多 {self.cfg['l2_keep_max']} 条，其余不要再标 +（它们会进流水账，这是正常的）。"
                hint = f"{hint} {extra}".strip()

            model_failed = False
            try:
                res = await self._summarize_group(group, summarize, channel, folks, hint)
            except Exception as e:  # 模型炸了不能连累其它组
                self.audit(f"MODEL_FAIL summarize {e}")
                model_failed = True
                res = None

            if res is not None:
                # 模型有时会回一段「抱歉/无法完成」：没有 ★/·/+ 标记，解析出来是一堆
                # 没有 sources 的"摘要"。这不能当"整理成功但零产出"，否则原文会被
                # 静默路由进 DL1，而不是留在 L2 等下一轮重压。
                usable = [s for s in (res.get("summaries") or []) if s.get("sources")]
                if not usable and not (res.get("keep") or []):
                    res = None

            if res is None:
                # 失败：整批回 L2，下轮还有机会再压（一条不丢）
                for it in group:
                    self._move(db, it, "L2", now)
                why = "model_fail" if model_failed else "empty_reply"
                self.audit(f"DIGEST_FALLBACK reason={why} {channel} {len(group)} 条 → L2（下轮再压）")
                continue

            sums = res.get("summaries") or []
            if not second_chance:
                sums = [s for s in sums if float(s.get("importance") or 0) >= 0.9]

            cited: set[str] = set()
            planned = []
            for s in sums:
                idxs = []
                for i in s.get("sources") or []:
                    try:
                        k = int(i)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= k <= len(group):
                        idxs.append(k)
                srcs = [group[i - 1] for i in idxs]
                if not srcs:
                    continue
                k = src_key(srcs)
                if any(x.get("srcKey") == k for x in db["DL3"]):
                    for it in srcs:
                        cited.add(it.get("id"))
                    continue
                item = self._new_summary(
                    text=str(s.get("text") or "").strip(),
                    channel=channel,
                    folks=folks,
                    scene=self._common_scene(srcs),
                    importance=float(s.get("importance") or 0.7),
                    now=now,
                )
                item["spanFrom"] = min(int(x.get("createdAt") or now) for x in srcs)
                item["spanTo"] = max(int(x.get("createdAt") or now) for x in srcs)
                item["sources"] = [x.get("id") for x in srcs]
                item["origin"] = origin
                item["srcKey"] = k
                planned.append((item, srcs))

            # 摘要先批量 embed —— fuse_summary 要靠向量做预筛（和 dsh 同顺序）
            if planned:
                await self._embed_items([p[0] for p in planned], embed_batch)
            for item, srcs in planned:
                outcome, target = await self.fuse_summary(db, item, now, judge_relation)
                if outcome == "merged" and target is not None:
                    # 合并可能把更长的正文写进旧摘要 → 旧向量作废，必须重算。
                    # dsh 侧同样处理（memory6 会先删掉旧 id 的向量再 embedInto）。
                    # 这边无条件重算：正文没变时多一次同文本请求，结果与 dsh 一致。
                    await self._embed_items([target], embed_batch, force=True)
                for it in srcs:
                    cited.add(it.get("id"))
                made += 1

            keep = set()
            for i in res.get("keep") or []:
                try:
                    k = int(i)
                except (TypeError, ValueError):
                    continue
                if 1 <= k <= len(group):
                    keep.add(group[k - 1].get("id"))

            # 先分桶，再一次性搬层：逐条 _move 是 O(N×层大小)，10 万条要 35 分钟
            to_dl2: list[dict] = []
            to_l2: list[dict] = []
            to_dl1: list[dict] = []
            for n_done, it in enumerate(group, 1):
                if started is not None and n_done % 2000 == 0:
                    self._check_timeout(started)
                if it.get("id") in cited:
                    to_dl2.append(it)
                elif it.get("id") in keep:
                    if not second_chance:
                        to_l2.append(it)
                    elif kept_total < int(self.cfg["l2_keep_max"]):
                        if it.get("layer") == "L2":
                            it["consolidatedAt"] = now   # 已在本层，只更新时间窗
                        else:
                            to_l2.append(it)
                        kept_total += 1
                    else:
                        to_dl1.append(it)
                else:
                    to_dl1.append(it)
            self._move_many(db, to_dl2, "DL2", now)
            self._move_many(db, to_l2, "L2", now)
            self._move_many(db, to_dl1, "DL1", now)

            # 这批已经被处理过（无论进 DL2 / L2 / DL1），不再是"等模型"的状态
            for it in group:
                it.pop("pendingDigest", None)

            await self._embed_items(group, embed_batch)
            self.audit(
                f"DIGEST {origin}/{scene or '-'}/{channel} {len(group)} 条 → 摘要 {made}（引用 {len(cited)}）"
            )
        return made

    async def _summarize_group(self, group, summarize, channel, folks, hint):
        """按 summary_chunk 切片调用摘要模型，片内编号合并时加回偏移。

        与 dsh 的 silicon.summarizeWithSources 分片等价：
          - 每片 ≤ summary_chunk（默认 60）条，编号从 1 重新开始
          - maxLines 按片长算（build_summary_prompt 内部 ceil(n/10)）
          - 某片失败只损失那一片：它的原文不会进 cited/keep，自然走 DL1（不丢）
        整组一次塞给模型在 10 万条时会 HTTP 400，这是 astrbot 侧原本缺的一层。
        """
        chunk = max(1, int(self.cfg.get("summary_chunk") or 60))

        def lines_of(items):
            # 带日期前缀：模型要靠它把「明天/下周三」折算成具体日期（与 dsh 同口径）
            return [raw_line(it) for it in items]

        if len(group) <= chunk:
            raw = await summarize(lines_of(group), channel, folks, hint)
            return parse_summary_reply(raw, len(group)) if isinstance(raw, str) else raw

        merged: dict = {"summaries": [], "keep": []}
        offset = 0
        slices = 0
        for i in range(0, len(group), chunk):
            part_group = group[i:i + chunk]
            slices += 1
            try:
                raw = await summarize(lines_of(part_group), channel, folks, hint)
            except Exception as e:
                # 单片失败只损失这一片（与 dsh 的 silicon 分片一致），其余片照常
                self.audit(f"MODEL_FAIL summarize slice={slices} {e}")
                raw = None
            part = parse_summary_reply(raw, len(part_group)) if isinstance(raw, str) else raw
            if not part:
                self.audit(
                    f"DIGEST_SLICE_FAIL {channel} slice={slices} offset={offset} n={len(part_group)}"
                )
                offset += len(part_group)
                continue
            for s in part.get("summaries") or []:
                srcs = []
                for x in s.get("sources") or []:
                    try:
                        srcs.append(int(x) + offset)
                    except (TypeError, ValueError):
                        continue
                merged["summaries"].append({**s, "sources": srcs})
            for x in part.get("keep") or []:
                try:
                    merged["keep"].append(int(x) + offset)
                except (TypeError, ValueError):
                    continue
            offset += len(part_group)
        self.audit(
            f"DIGEST_SLICE {channel} {len(group)} 条 → {slices} 片 / "
            f"{len(merged['summaries'])} 摘要 / {len(merged['keep'])} keep（chunk={chunk}）"
        )
        return merged

    # ---------- 两层整理 ----------

    async def refine_importance(self, items: list[dict], classify, chunk: int = 20) -> int:
        """对"还没分类"的条目批量问模型，补 channel 与 importance。

        为什么放在整理时而不是写入时：
          ① 写入路径上每多一次模型往返，就多一秒延迟；整理是后台的，等得起
          ② 刚写进来的条目可能马上被合并/作废/遗忘，那时候分类纯属浪费
          ③ 整批 20 条一次请求，成本约 1/20

        为什么必须有：channel 决定摘要用哪套 _FOCUS（"做过的事"和"说过的话"
        重点不同），importance 决定检索打分和 DL1 的遗忘速度。全都恒为
        dialog/0.5 的话，这两套机制形同虚设。

        模型不可用 / 解析失败时**保留 impRough 标记**，下一轮再试；
        不会用本地正则假装分过类（那只会让"没分类"看起来像"分类过了"）。
        """
        rough = [it for it in (items or []) if it.get("impRough")]
        if not rough:
            return 0
        if classify is None:
            self.audit(f"MODEL_MISSING classify {len(rough)} 条（保留默认 dialog/0.5，下轮再试）")
            return 0

        done = 0
        changed = 0
        for i in range(0, len(rough), chunk):
            part = rough[i : i + chunk]
            texts = [str(it.get("text") or "")[:400] for it in part]
            try:
                got = await classify(texts)
            except Exception as e:
                self.audit(f"MODEL_FAIL classify chunk={len(part)} {e}")
                continue
            if not isinstance(got, list) or len(got) != len(part):
                self.audit(f"MODEL_FAIL classify chunk={len(part)} 返回格式不对，保留待分类")
                continue
            for it, t in zip(part, got):
                if not isinstance(t, dict):
                    continue
                try:
                    imp = float(t.get("importance"))
                except (TypeError, ValueError):
                    continue
                if abs(imp - float(it.get("importance") or 0)) > 1e-9:
                    changed += 1
                it["importance"] = imp
                ch = str(t.get("channel") or "")
                if ch in CHANNELS:
                    it["channel"] = ch
                it.pop("impRough", None)
                done += 1
        self.audit(f"CLASSIFY {done}/{len(rough)} 条（改判 {changed}，chunk={chunk}）")
        return done

    async def organize_l1(self, db, summarize, started: int, *, embed_batch=None, judge_relation=None, classify=None) -> int:
        if _chars(db["L1"]) <= self.cfg["hot_max_chars"] and len(db["L1"]) <= self.cfg["hot_max_items"]:
            return 0
        batch = pick_turn_batch(
            db["L1"],
            self.cfg["hot_max_chars"],
            order_key=lambda it: it.get("createdAt") or 0,
            quiet_before=_now() - self.cfg["quiet_ms"],
            max_items=self.cfg["hot_max_items"],
        )
        if not batch:
            self.audit("L1_FULL_NO_QUIET 都在聊，等安静了再整理")
            return 0
        self._check_timeout(started)
        # ★ 在 digest 之前分类：channel 参与 _group_of 分组，
        #   分在分组之后等于本轮还是按 dialog 合批，白分。
        await self.refine_importance(batch, classify)
        await self.digest(
            db, batch, summarize, second_chance=False,
            embed_batch=embed_batch, judge_relation=judge_relation,
            started=started,
        )
        return len(batch)

    async def organize_l2(self, db, summarize, started: int, *, embed_batch=None, judge_relation=None, classify=None) -> int:
        if (
            _chars(db["L2"]) <= self.cfg["consolidate_max_chars"]
            and len(db["L2"]) <= self.cfg["consolidate_max_items"]
        ):
            return 0
        due = pick_turn_batch(
            db["L2"],
            self.cfg["consolidate_max_chars"],
            order_key=lambda it: it.get("consolidatedAt") or it.get("createdAt") or 0,
            max_items=self.cfg["consolidate_max_items"],
        )
        if not due:
            return 0
        self._check_timeout(started)
        batch = list(due) + list(db["DL1"])  # DL1 与 L2 旧半一起处理
        await self.refine_importance(batch, classify)
        await self.digest(
            db, batch, summarize, second_chance=True,
            embed_batch=embed_batch, judge_relation=judge_relation,
            started=started,
        )
        return len(batch)

    def _check_timeout(self, started: int) -> None:
        if _now() - started > float(self.cfg["max_organize_ms"]):
            raise TimeoutError("整理超时")

    # ---------- 遗忘 ----------

    # ---------- 信号（attend / pin / forget） ----------

    def _write_forgotten(self, it: dict, now: int, retention) -> bool:
        """遗忘前先写台账。写不进去就不删（宁可留着）。"""
        rec = {
            "id": it.get("id"),
            "layer": it.get("layer"),
            "channel": it.get("channel"),
            "text": str(it.get("text") or "")[:240],
            "importance": it.get("importance"),
            "created_local": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(float(it.get("createdAt") or now) / 1000)
            ),
            "forgotten_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now / 1000)),
            "lifespan_days": round((now - float(it.get("createdAt") or now)) / DAY),
            "retention": None if retention is None else round(float(retention), 3),
            "reason": "遗忘",
        }
        try:
            os.makedirs(self.grave_dir, exist_ok=True)
            with open(os.path.join(self.grave_dir, "forgotten.jsonl"), "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False

    def claim_signals(self) -> list[str]:
        """认领别人的信号文件（rename 成功才算收下）。"""
        sig_dir = os.path.join(self.dir, "signals")
        claimed: list[str] = []
        try:
            names = sorted(os.listdir(sig_dir))
        except OSError:
            return claimed
        now = _now()
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            src = os.path.join(sig_dir, name)
            dst = os.path.join(sig_dir, f"{name[:-6]}.claimed-{now}-{random.randrange(1 << 16):04x}.jsonl")
            try:
                os.rename(src, dst)
                claimed.append(dst)
            except OSError:
                continue
        return claimed

    def _read_jsonl(self, path: str) -> list[dict]:
        out: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        out.append(obj)
        except OSError:
            return out
        return out

    @staticmethod
    def _find_any(db: dict, item_id: str) -> dict | None:
        for l in ("DL3", "DL2", "DL1", "L2", "L1"):
            for it in db[l]:
                if it.get("id") == item_id:
                    return it
        return None

    def apply_signals(self, db: dict) -> dict:
        """消费 attend / pin / forget。opId 记账防重放（applied.json）。"""
        claimed = self.claim_signals()
        if not claimed:
            return {"ops": 0, "claimed": [], "applied": None}
        applied_path = os.path.join(self.dir, "signals", "applied.json")
        try:
            with open(applied_path, "r", encoding="utf-8") as f:
                applied = json.load(f)
        except (OSError, json.JSONDecodeError):
            applied = {}
        done = set(applied.get("ops") or [])
        now = _now()
        n = 0
        for p in claimed:
            for op in self._read_jsonl(p):
                op_id = op.get("opId")
                if not op_id or op_id in done:
                    continue
                ids = op.get("ids") if isinstance(op.get("ids"), list) else []
                kind = op.get("op")
                if kind == "attend":
                    for i in ids:
                        it = self._find_any(db, i)
                        if not it:
                            continue
                        touch_heat(it, now, self.cfg)
                        it["recallCount"] = int(it.get("recallCount") or 0) + 1
                        it["lastRecalledAt"] = now   # 衰减从这里起算
                        if it.get("layer") == "DL1":
                            self._move(db, it, "L2", now)
                        if it.get("layer") == "DL3":
                            self._l3_dirty = True  # 热度变了，L3 排序要跟着变
                        n += 1
                elif kind == "pin":
                    for i in ids:
                        it = self._find_any(db, i)
                        if it:
                            it["pinned"] = bool(op.get("v"))
                            n += 1
                elif kind == "forget":
                    doomed: list[str] = []
                    for i in ids:
                        it = self._find_any(db, i)
                        if not it:
                            continue
                        if not self._write_forgotten(it, now, None):
                            self.audit(f"FORGET_FAIL {i} 台账写不进去，不删")
                            continue
                        if it.get("layer") == "DL3":
                            self._l3_dirty = True
                        doomed.append(i)
                        if self.vectors is not None:
                            self.vectors.forget(i)
                        n += 1
                    if doomed:
                        # 一批 op 里删多条时只扫一遍层，不要每条都重建 5 个 list
                        dset = set(doomed)
                        for l in DATA_LAYERS:
                            db[l] = [x for x in db[l] if x.get("id") not in dset]
                done.add(op_id)
        applied["ops"] = list(done)[-2000:]
        return {"ops": n, "claimed": claimed, "applied": applied}

    def forget_dl1(self, db) -> int:
        now = _now()
        keep = []
        n = 0
        for it in db["DL1"]:
            if it.get("pinned"):
                keep.append(it)
                continue
            # 等模型整理的原文：还没压过摘要，忘了就真丢了
            if it.get("pendingDigest"):
                keep.append(it)
                continue
            r = retention_of(it, now, self.cfg)
            if r >= float(self.cfg["forget_threshold"]):
                keep.append(it)
                continue
            if not self._write_forgotten(it, now, r):
                keep.append(it)  # 台账写不进去就不删
                continue
            # 遗忘要连向量一起删：留着就是孤儿向量，占空间还会被 fuse 预筛扫到
            if self.vectors is not None:
                self.vectors.forget(it.get("id"))
            n += 1
        db["DL1"] = keep
        if n:
            self.audit(f"FORGET {n} 条（DL1 保持率低于 {self.cfg['forget_threshold']}）")
        return n

    # ---------- L3 快照 ----------

    def dl3_version(self) -> str:
        try:
            st = os.stat(self.path("DL3"))
            return f"{int(st.st_mtime * 1000)}:{st.st_size}"
        except OSError:
            return "0:0"

    def compute_l3(self, db: dict | None = None, now: int | None = None) -> list[dict]:
        now = now or _now()
        pool = db["DL3"] if db else self.read_layer("DL3")
        scored = [
            (it, (float(it.get("importance") or 0)) * heat_of(it, now, self.cfg))
            for it in pool
            if not it.get("supersededBy")
        ]
        scored.sort(key=lambda x: -x[1])
        out = []
        used = 0
        for it, _s in scored:
            ln = len(str(it.get("text") or ""))
            if used + ln > float(self.cfg["long_max_chars"]):
                break
            out.append(it)
            used += ln
        return out

    def refresh_l3(self, db: dict | None = None) -> int:
        items = self.compute_l3(db)
        payload = {
            "version": 3,
            "at": _now(),
            "dl3v": self.dl3_version(),
            "items": items,
        }
        self.atomic_write(self.path("L3"), json.dumps(payload, ensure_ascii=False, indent=1))
        self._last_l3_at = _now()
        self._l3_dirty = False
        return len(items)

    def l3_snapshot_stale(self) -> bool:
        """快照是否落后于 DL3（整理者据此决定要不要重写）。"""
        try:
            with open(self.path("L3"), "r", encoding="utf-8") as f:
                snap = json.load(f)
            return snap.get("dl3v") != self.dl3_version()
        except (OSError, json.JSONDecodeError):
            return True

    # ---------- 向量（阶段 D） ----------

    async def _embed_items(self, items, embed_batch, force: bool = False) -> int:
        """给条目补向量（只补还没有的，force=True 时重算）。"""
        store = self.vectors
        if store is None or embed_batch is None or not items:
            return 0
        ok, why = store.writable()
        if not ok:
            self.audit(f"VEC_SKIP {why}")
            return 0
        todo = []
        for it in items:
            i = it.get("id")
            if not i or not str(it.get("text") or "").strip():
                continue
            if not force and store.get(i):
                continue
            todo.append(it)
        if not todo:
            return 0
        chunk_size = max(1, int(self.cfg.get("embed_chunk") or 32))
        concurrency = max(1, int(self.cfg.get("embed_concurrency") or 4))
        chunks = [todo[i:i + chunk_size] for i in range(0, len(todo), chunk_size)]
        sem = asyncio.Semaphore(concurrency)

        async def one(chunk: list[dict]) -> tuple[list[dict], list]:
            async with sem:
                texts = [str(it.get("text") or "")[:512] for it in chunk]
                try:
                    return chunk, await embed_batch(texts)
                except Exception as e:
                    # 一批失败只丢这一批（32 条），不连累整组
                    self.audit(f"MODEL_FAIL embed chunk={len(chunk)} {e}")
                    return chunk, []

        n = 0
        for chunk, got in await asyncio.gather(*(one(c) for c in chunks)):
            for it, v in zip(chunk, got or []):
                if v:
                    store.remember(it.get("id"), v)
                    n += 1
        return n

    async def sync_vectors(self, db: dict, embed_batch, limit: int = 200) -> int:
        """把还没有向量的条目批量补上。只有拿到钥匙时调用（避免两个写者）。"""
        store = self.vectors
        if store is None or embed_batch is None:
            return 0
        ok, why = store.writable()
        if not ok:
            self.audit(f"VEC_SKIP {why}")
            return 0
        pool = []
        for l in ("DL3", "DL2", "DL1", "L2", "L1"):
            pool.extend(db[l])
        todo = store.missing_ids(pool, limit=limit)
        if not todo:
            return 0
        texts = [str(it.get("text") or "")[:512] for it in todo]
        try:
            got = await embed_batch(texts)
        except Exception as e:
            self.audit(f"VEC_EMBED_FAIL {e}")
            return 0
        pairs = {}
        for it, v in zip(todo, got or []):
            if v:
                pairs[it.get("id")] = [float(x) for x in v]
        if not pairs:
            return 0
        try:
            n = store.put(pairs)
        except OSError as e:
            self.audit(f"VEC_WRITE_FAIL {e}")
            return 0
        self.audit(f"VEC_ADD {n} 条")
        return n

    # ---------- 主流程 ----------

    async def organize(
        self,
        summarize: Callable[..., Awaitable[Any]] | None = None,
        embed_batch: Callable[..., Awaitable[Any]] | None = None,
        judge_relation: Callable[..., Awaitable[Any]] | None = None,
        classify: Callable[..., Awaitable[Any]] | None = None,
    ) -> dict:
        """一轮整理。顺序严格照 ARCHITECTURE.md 第 26 节 / memory6.organize()：

        reconcile → mergeTemps → applySignals → reconcile
        → organizeL1 → organizeL2 → forgetDL1 → checkLock
        → saveDb(先深层后 L1) → 写 applied → 删源 → 落向量 → 视需要刷新 L3 → 放锁
        """
        if not self.acquire_lock():
            return {"ok": False, "skipped": "busy"}

        started = _now()
        claimed: list[str] = []
        sig_claimed: list[str] = []
        try:
            db = self.load_db()
            rec1 = self.reconcile(db)

            # DL3 的内容快照：用来判断这轮到底动没动摘要，没动就别重写文件
            # （重写会让 mtime 变，让对方把 L3 快照判过期，白白打断一次前缀缓存）
            dl3_before = json.dumps(db["DL3"], ensure_ascii=False, sort_keys=True)

            m = self.merge_temps(db)
            claimed = m["claimed"]

            # 先消费信号：attend 可能把 DL1 升回 L2，别让 L2 摘要先把这条吃掉
            sg = self.apply_signals(db)
            sig_claimed = sg["claimed"]
            # 信号可能刚删了摘要；再对一次账，DL2 孤儿同轮就归位到 DL1
            rec2 = self.reconcile(db)
            self._warn_layers(db)
            self._check_timeout(started)

            # L1 始终跑：没有模型时它不产生摘要，只把溢出的批次下沉到 DL1
            # （DL1 不进上下文，也不参与遗忘，等有模型后 L2 满时一起压）。
            n1 = await self.organize_l1(
                db, summarize, started,
                embed_batch=embed_batch, judge_relation=judge_relation,
                classify=classify,
            )
            # L2 只在有模型时跑：没有模型就别把缓冲层清空，留着等能压的时候。
            n2 = (
                await self.organize_l2(
                    db, summarize, started,
                    embed_batch=embed_batch, judge_relation=judge_relation,
                    classify=classify,
                )
                if summarize
                else 0
            )
            nf = self.forget_dl1(db)

            if not self.check_lock():
                raise RuntimeError("钥匙被接管，放弃本轮")

            changed = (
                m["merged"] > 0 or sg["ops"] > 0 or n1 > 0 or n2 > 0 or nf > 0
                or rec1 > 0 or rec2 > 0
            )
            dl3_changed = json.dumps(db["DL3"], ensure_ascii=False, sort_keys=True) != dl3_before
            if changed:
                self.save_db(db, skip=() if dl3_changed else ("DL3",))

            # 顺序：先存数据，再记 applied，再删源文件（崩在中间最多重放一次，不会丢）
            if sg["applied"]:
                self.atomic_write(
                    os.path.join(self.dir, "signals", "applied.json"),
                    json.dumps(sg["applied"], ensure_ascii=False, indent=1),
                )
            for p in claimed + sig_claimed:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            claimed, sig_claimed = [], []

            # 向量：digest 里已经 remember 进内存，这里统一落盘
            nv = self.vectors.flush() if self.vectors is not None else 0

            # L3 快照：内容变了 / 快照过期 / 到了刷新间隔 才重写
            l3_every = float(self.cfg["view_refresh_ms"] or 0)
            need_l3 = (
                self._l3_dirty
                or not l3_every
                or (_now() - self._last_l3_at >= l3_every)
                or self.l3_snapshot_stale()
            )
            l3 = self.refresh_l3(db) if need_l3 else len(self.read_layer_safe("L3"))

            state = {
                "at": _now(),
                "writer": self.writer,
                "elapsedMs": _now() - started,
                "merged": m["merged"],
                "signals": sg["ops"],
                "l1Moved": n1,
                "l2Moved": n2,
                "forgotten": nf,
                "vectors": nv,
                "l3": l3,
                "count": {l: len(db[l]) for l in DATA_LAYERS},
                "chars": {l: _chars(db[l]) for l in DATA_LAYERS},
            }
            self.atomic_write(self.state_path, json.dumps(state, ensure_ascii=False, indent=1))
            self.audit(
                f"ORGANIZE ok merged={m['merged']} sig={sg['ops']} L1={n1} L2={n2} "
                f"forget={nf} vec={nv} L3={l3} {state['elapsedMs']}ms"
            )
            return {"ok": True, **state}
        except Exception as e:
            self.audit(f"ORGANIZE_FAIL {e}")
            return {"ok": False, "error": str(e)}
        finally:
            # ★ 失败时**不动** claimed 文件：里面是还没落盘的原文，
            #   留在 L1.tmp 下次整理会被重新认领（按 id 去重，不会重复）。
            self.release_lock()

    # ---------- 状态 ----------

    def state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def lock_owner(self) -> dict | None:
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None


# 判关系 prompt：逐字照抄 dsh 的 silicon.relation（29 节「判关系模型不稳定」的补丁也在里面）
RELATION_SYSTEM_PROMPT = "\n".join([
    "你判断两条记忆的关系，只输出一个词。",
    "",
    "duplicate      同一件事，只是措辞不同。「别发表情」和「不要发表情包」",
    "               同一条规则被重复强调也算 duplicate（哪怕措辞、语气、补充细节不同）",
    "contradiction  后一条推翻了前一条。「别发表情」和「多发表情」",
    "update         后一条是前一条的进展或补充，不冲突。",
    "               「Dice 0.86」和「Dice 涨到 0.88」不是矛盾，是进展。",
    "               「补了 64 例标注」和「补到 128 例」也是进展。",
    "unrelated      两件不同的事。",
    "",
    "只输出这四个词之一，不要解释。",
])


def parse_relation(out: str) -> str | None:
    w = str(out or "").strip().lower()
    for k in ("contradiction", "duplicate", "update", "unrelated"):
        if k in w:
            return k
    return None


# 分类 prompt：逐字照抄 dsh 的 silicon.CLASSIFY_SYS。
#
# 为什么这里必须调模型、而不是拿正则判：本地规则只认得出「困/累」和
# 「跑通+数字」这两头，中间那一大片（「他说他最怕漏诊」这种原则诉求、
# 「刷到一条视频」这种外部事件）判不出来，全被塞进 dialog/0.5。
# channel 进摘要 prompt 的 _FOCUS 分支，importance 进检索打分和遗忘曲线，
# 判错不是"标签不好看"，是把不该忘的忘掉、把「做过的事」按「说的话」去总结。
CLASSIFY_SYSTEM_PROMPT = "\n".join([
    "你给一条记忆打两个标签，输出一行 JSON。",
    "",
    "channel 三选一：",
    "  action = 自己动手做了一件具体的事。跑通了、删掉 37 条、补完标注、部署了。",
    "  dialog = 说了一段话，重点在话本身。他说他最怕漏诊、我吐槽像年糕、聊到爬山。",
    "  event  = 外面发生的事，不是自己主动做的。刷到一条视频、看到有人吵架、路上被撞、群里出事。",
    "  分不清 action 和 event 时：自己动手 = action，外面发生的 = event。都不像就选 dialog。",
    "  别人说的话（转述、要求、抱怨、答应）都算 dialog，哪怕内容是命令——",
    "  「老王说周五之前必须交报告」是 dialog，不是 event。",
    "  event 只用于「没人说、自己也没做，就是发生了」的事：刷到视频、路上被撞。",
    "",
    "importance 三档：0.85 定原则或长期诉求；0.6 有结果的具体事件；0.25 闲聊琐事。",
    "",
    "★ 比例很重要：绝大多数记忆是 0.6 或 0.25。",
    "  0.85 只给「以后都这样做」的规则、禁令、长期偏好，一天里通常不超过一两条。",
    "  不确定时给 0.6，不要给 0.85。",
    "  「做了某件事、拿到了某个结果」是 0.6，不是 0.85——哪怕结果很重要。",
    "",
    "示例：",
    '记忆：以后评估一律以召回率为准  → {"importance":0.85,"channel":"dialog"}',
    '记忆：以后叫我小鱼就行，别叫全名  → {"importance":0.85,"channel":"dialog"}',
    '记忆：主人说他最怕漏诊，宁可多报几个  → {"importance":0.85,"channel":"dialog"}',
    '记忆：把后处理加了连通域过滤，Dice 涨了 0.02  → {"importance":0.6,"channel":"action"}',
    '记忆：下午补完了 64 例标注  → {"importance":0.6,"channel":"action"}',
    '记忆：刷抖音刷到一个讲肺结节的，说得挺在理  → {"importance":0.6,"channel":"event"}',
    '记忆：在抖音跟人吵了一架，吵的啥记不清了  → {"importance":0.6,"channel":"event"}',
    '记忆：群里聊到爬山，我说看天气  → {"importance":0.25,"channel":"dialog"}',
    '记忆：今天有点困  → {"importance":0.25,"channel":"dialog"}',
    "",
    "只输出 JSON，不要解释。",
])

IMPORTANCE_LEVELS = (0.85, 0.6, 0.25)
CHANNELS = ("dialog", "action", "event", "note")

# 批量分类 prompt：逐字照抄 dsh 的 silicon.CLASSIFY_BATCH_SYS。
# 一次请求打 N 条，成本约 1/N；单条调用在整理 40 条时就是 40 次往返。
CLASSIFY_BATCH_SYSTEM_PROMPT = "\n".join([
    "你给一批记忆各打两个标签，输出一个 JSON 数组。",
    "",
    "channel 三选一：",
    "  action = 自己动手做了一件具体的事。跑通、删掉 37 条、补完标注、部署。",
    "  dialog = 说了一段话，重点在话本身。他说他最怕漏诊、我吐槽像年糕、聊到爬山。",
    "  event  = 外面发生的事，不是自己主动做的。刷到视频、看到吵架、路上被撞。",
    "  分不清 action 和 event 时：自己动手 = action，外面发生的 = event。都不像就选 dialog。",
    "  别人说的话（转述、要求、抱怨、答应）都算 dialog，哪怕内容是命令——",
    "  「老王说周五之前必须交报告」是 dialog，不是 event。",
    "  event 只用于「没人说、自己也没做，就是发生了」的事：刷到视频、路上被撞。",
    "",
    "importance 三档：0.85 定原则或长期诉求；0.6 有结果的具体事件；0.25 闲聊琐事。",
    "",
    "★ 比例很重要：一批里绝大多数应该是 0.6 或 0.25，0.85 通常只有一两条。",
    "  0.85 只给「以后都这样做」的规则、禁令、长期偏好。",
    "  不确定时给 0.6，不要给 0.85。",
    "  做了某件事、拿到了某个结果（跑通、删掉 37 条、补完 128 例）是 0.6，不是 0.85。",
    "  每条独立判断，不要因为前一条给了 0.85 就跟着给。",
    "",
    "输出格式（每项必须带 i，i 就是输入的编号）：",
    '[{"i":1,"importance":0.85,"channel":"dialog"},{"i":2,"importance":0.25,"channel":"dialog"}]',
    "",
    "规则：",
    "  - 输入的每一条都要有一项，不能漏、不能多。",
    "  - 顺序无关，靠 i 对齐。",
    "  - 示例：",
    '    1. 以后评估一律以召回率为准  → {"i":1,"importance":0.85,"channel":"dialog"}',
    '    2. 把后处理加了连通域过滤，Dice 涨了 0.02  → {"i":2,"importance":0.6,"channel":"action"}',
    '    3. 刷抖音刷到一个讲肺结节的  → {"i":3,"importance":0.6,"channel":"event"}',
    '    4. 今天有点困  → {"i":4,"importance":0.25,"channel":"dialog"}',
    "",
    "只输出 JSON 数组，不要解释。",
])

_RESULT_PATTERN = re.compile(
    r"(跑通|改完|部署|提交|补完|删掉|清理|扩容|优化|压到|涨到|降到|过了|做完|写完|跑完|推进|上线)"
    r"[^。，；]{0,24}?(\d|一半|翻倍|四成|三成|两倍)"
)
_HIGH_PATTERN = re.compile(r"(以后|从今|一律|永远|不许|不准|不要|必须|只能|记住|原则|标准是|我说了算)")


def parse_classify_reply(raw: str, text: str = "") -> dict | None:
    """解析分类回复。解析不出来返回 None（调用方保留默认值、下轮重试）。

    `text` 是被分类的原文——两条硬规则要在原文上匹配，不能拿模型回复匹配
    （回复里只有 JSON，永远匹配不到「涨到 0.88」这种结果句）。
    """
    s = str(raw or "").strip()
    if not s:
        return None
    m = re.search(r"\{[\s\S]*?\}", s)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    try:
        imp = float(j.get("importance"))
    except (TypeError, ValueError):
        return None
    ch = str(j.get("channel") or "").strip()
    if ch not in ("dialog", "action", "event"):
        ch = "dialog"
    # 模型给的值常常不在三档里，归到最近的档（与 dsh 同口径）
    imp = min(IMPORTANCE_LEVELS, key=lambda x: abs(x - imp))
    body = str(text or "")
    # 有数字结果的具体事件，至少 0.6——模型经常把这类判成 0.25
    if _RESULT_PATTERN.search(body) and imp < 0.6:
        imp = 0.6
    # 明确的长期诉求，抬到原则档——模型经常低估这类
    if _HIGH_PATTERN.search(body) and imp < 0.85:
        imp = 0.85
    return {"importance": imp, "channel": ch}


def parse_classify_batch(raw: str, texts: list[str]) -> list[dict | None]:
    """解析批量分类回复，返回与 texts 等长的列表；解析不出的位置是 None。

    模型有时漏项、有时顺序乱，所以先按 `i` 对齐、再退回按位置。
    （照 dsh 的 `classifyBatch`：`arr.find(x => Number(x?.i) === k+1) || arr[k]`）
    """
    n = len(texts)
    out: list[dict | None] = [None] * n
    s = str(raw or "").strip()
    if not s:
        return out
    m = re.search(r"\[[\s\S]*\]", s)
    if not m:
        return out
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return out
    if not isinstance(arr, list):
        return out

    # 有 i 就严格按 i 对齐；**不能**在缺项时退回按位置——
    # 漏了第 2 项却拿第 3 项的标签顶上，等于把两件事的记忆互相污染。
    has_i = any(isinstance(x, dict) and "i" in x for x in arr)
    for k, text in enumerate(texts):
        if has_i:
            item = next(
                (x for x in arr if isinstance(x, dict) and str(x.get("i")) == str(k + 1)),
                None,
            )
        else:
            item = arr[k] if k < len(arr) and isinstance(arr[k], dict) else None
        if not item:
            continue
        try:
            imp = float(item.get("importance"))
        except (TypeError, ValueError):
            continue
        ch = str(item.get("channel") or "").strip()
        if ch not in ("dialog", "action", "event"):
            ch = "dialog"
        imp = min(IMPORTANCE_LEVELS, key=lambda x: abs(x - imp))
        body = str(text or "")
        if _RESULT_PATTERN.search(body) and imp < 0.6:
            imp = 0.6
        if _HIGH_PATTERN.search(body) and imp < 0.85:
            imp = 0.85
        out[k] = {"importance": imp, "channel": ch}
    return out


_FOCUS = {
    "action": "重点：做了什么、结果如何、数字变化。",
    "event": "重点：发生了什么、在哪、大概经过。细节可以模糊，但「发生过这件事」这个骨架必须留住。",
    "dialog": "重点：谁说了什么、定了什么、聊到哪了。特别注意对方表达的原则和长期诉求，这类一定要留下。",
}


def build_summary_prompt(lines, channel: str, folks, hint: str = "") -> tuple[str, str]:
    """摘要 prompt（逐字照抄 dsh 的 `silicon.summarizeWithSources`）。

    返回 (system_prompt, user_prompt)。条数上限是"刹车"不是"指标"，
    这条规则是 dsh 反复调出来的，别改。
    """
    import math

    lst = [str(x or "").strip() for x in (lines or [])]
    lst = [x for x in lst if x]
    n = len(lst)
    focus = _FOCUS.get(channel, "重点：发生了什么、得出什么结论。")
    ppl = [str(x) for x in (folks or []) if x]
    max_lines = max(2, math.ceil(n / 10))

    sys_prompt = "你把几条记忆压成摘要，并给每条摘要标明依据了哪几条原文。"
    parts = [
        f"要求：{hint}" if hint else "",
        focus,
        "保留人名和具体数字。不许写原文里没有的事。",
        "原文行首的 [月-日] 是这一条发生的时间。原文里的「今天/明天/昨天/下周/上周三」"
        "要按那个日期折算成具体日期再写进摘要（例如 10-30 说的「明天」写成「10-31」）；"
        "原文里已有的日期照抄。摘要里不要出现「今天」这种离开原文就看不懂的词。",
        f"这批里出现过的人：{'、'.join(ppl)}。摘要里写到具体的事，必须带上是谁说的/谁做的。"
        if ppl
        else "原文里如果有人名，摘要里也要留着。",
        "",
        f"这一批共 {n} 条，最多写 {max_lines} 条摘要——这是上限，不是任务量，写得比它少完全可以。",
        f"超过 {max_lines} 条的摘要会被直接丢弃（白写），请务必合并。",
        "一条摘要只讲一件事。两件不相干的事，**宁可各自不写，也绝不能挤进同一行**。",
        "同一件事的几句话合成一条，只写结论，不要把每句都复述一遍。",
        "单独拎出来没什么信息量的（打招呼、附和、嗯哦啊），不要为它写摘要。",
        "**大部分内容不该写摘要**——它们会作为原文沉到缓冲层或流水账，这是设计好的。",
        "只有真正值得长期记住的才写摘要。",
        "",
        "输出格式——每条摘要一行，行首用符号标重要性：",
        "",
        "★ 摘要正文（不超过40字）《1,3,5》",
        "· 摘要正文（不超过40字）《4》",
        "+ 2,7",
        "",
        "三个符号的区别：",
        "  ★ 特别重要 —— 原则、命令、长期承诺、关键结果、「以后一律…」。必须长期记住。",
        "  · 一般重要 —— 有信息量的日常，记着有好处，但不强制。",
        "  + 不用写摘要 —— 有信息量、值得留着以后查的（约定、数字、人名、时间地点）。",
        "",
        "★ 应该是少数，大部分值得写摘要的用 · 就行。",
        "**+ 要少（约一到两成，宁少勿多）**：只有真有信息量的才标，进了缓冲层比流水账留得久。",
        "打招呼、语气词、闲聊碎语、当下状态这些不重要的**不要标 +**，它们落进流水账、",
        "很快忘掉是正常的，别什么都往缓冲层塞。",
        "《》里填这条摘要依据了原文的哪几个编号，逗号分隔，连续的可以写成 1-3。",
        "一条摘要引用的编号，必须是在说同一件事。**编号之间毫无关系，就说明你在硬凑。**",
        "编号必须是上面真实存在的数字，不许编。",
        "没被这些行用到的条目就让它闲着——它们会被降级处理，这是设计好的。",
        "没有值得 + 的条目就不写 + 那行。不要为了凑数乱标。",
        "",
        "原文：",
        *[f"{i + 1}. {t}" for i, t in enumerate(lst)],
    ]
    return sys_prompt, "\n".join([p for p in parts if p != ""])


def purge_graveyard(dir_path: str) -> int:
    """（运维用）把 graveyard 清掉，返回删除的文件数。"""
    g = os.path.join(dir_path, "graveyard")
    n = 0
    if os.path.isdir(g):
        for name in os.listdir(g):
            p = os.path.join(g, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.unlink(p)
                n += 1
            except OSError:
                pass
    return n


__all__ = [
    "Organizer",
    "RELATION_SYSTEM_PROMPT",
    "parse_relation",
    "ORG_DEFAULTS",
    "pick_turn_batch",
    "parse_summary_reply",
    "parse_index_list",
    "retention_of",
    "heat_of",
    "half_life_of",
    "purge_graveyard",
]
