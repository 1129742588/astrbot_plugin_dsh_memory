"""阶段 D：向量 + 语义检索。

与 dsh **共用同一份向量文件**（格式必须一模一样，否则 dsh 会判定 stale 把整个库丢掉）：

```
vectors/deep-part-0000.json   {"<id>": "<base64 of Float32Array>"}
vectors/deep-meta.json        {"sig":"api:BAAI/bge-m3","dim":1024,"count":N,"at":"..."}
```

三条硬约束：

1. **嵌入模型必须和 dsh 完全一致**。meta 里的 `sig` 对不上就只读不写——
   写了会让对方把整个库当成"换了模型"，旧向量全部作废。
2. float32 小端（JS 的 Float32Array 就是平台字节序，x86 是小端）。
3. 只有拿到 `organize.lock` 的一方写向量，避免两个写者互相覆盖。
"""

from __future__ import annotations

import base64
import json
import math
import os
import struct
import time
from typing import Iterable

VEC_FILE = "deep-part-0000.json"
VEC_META = "deep-meta.json"

# dsh 的嵌入签名格式：`api:<model>`；本地哈希向量是 `local:<n>` 之类
def signature_of(model: str) -> str:
    return f"api:{model}"


def encode(vec: Iterable[float]) -> str:
    values = [float(x) for x in vec]
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")


def decode(b64: str) -> list[float]:
    raw = base64.b64decode(b64)
    n = len(raw) // 4
    return list(struct.unpack(f"<{n}f", raw[: n * 4]))


def cosine(a: list[float], b: list[float]) -> float:
    # 维度不一致直接判 0：与 dsh 的 cosine 一致。截断后照算会给出假相关性。
    if not a or not b or len(a) != len(b):
        return 0.0
    n = len(a)
    dot = na = nb = 0.0
    for i in range(n):
        x = a[i]
        y = b[i]
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


class VectorStore:
    """一个数据目录的向量库。和 dsh 抢同一个文件，但只在持锁时写。"""

    def __init__(self, dir_path: str, model: str, dim: int = 0) -> None:
        self.dir = os.path.abspath(os.path.expanduser(str(dir_path)))
        self.vec_dir = os.path.join(self.dir, "vectors")
        self.vec_path = os.path.join(self.vec_dir, VEC_FILE)
        self.meta_path = os.path.join(self.vec_dir, VEC_META)
        self.sig = signature_of(model) if model else ""
        self.dim = int(dim or 0)
        self._cache: dict[str, list[float]] | None = None
        self._cache_sig = ""
        self._dirty = False

    # ---------- 元数据 ----------

    def meta(self) -> dict:
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def writable(self) -> tuple[bool, str]:
        """能不能往这个库写。不能写就别写——会把对方的向量空间搞废。"""
        if not self.sig:
            # 没有模型名就写不出正确的 sig，写进去 dsh 会判 stale 并丢掉整库
            return False, "没有配置嵌入模型"
        m = self.meta()
        if not m:
            return True, "首次建库"
        if m.get("sig") != self.sig:
            return False, f"签名不一致：库里是 {m.get('sig')}，我这边是 {self.sig}"
        return True, ""

    # ---------- 读写 ----------

    def load(self) -> dict[str, list[float]]:
        try:
            st = os.stat(self.vec_path)
            sig = f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            # 文件还不存在：只在首次初始化，别把本进程刚 remember 的东西冲掉
            if self._cache is None:
                self._cache = {}
            self._cache_sig = ""
            return self._cache
        if self._cache is not None and sig == self._cache_sig:
            return self._cache
        out: dict[str, list[float]] = {}
        try:
            with open(self.vec_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for k, b64 in (raw or {}).items():
                if not isinstance(b64, str):
                    continue
                try:
                    out[k] = decode(b64)
                except (ValueError, struct.error):
                    continue
        except (OSError, json.JSONDecodeError):
            out = {}
        self._cache = out
        self._cache_sig = sig
        return out

    def save(self, vectors: dict[str, list[float]]) -> bool:
        ok, why = self.writable()
        if not ok:
            return False
        os.makedirs(self.vec_dir, exist_ok=True)
        payload = {k: encode(v) for k, v in vectors.items()}
        tmp = f"{self.vec_path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f)
        last = None
        for _ in range(6):
            try:
                os.replace(tmp, self.vec_path)
                break
            except OSError as e:
                last = e
                time.sleep(0.03)
        else:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise OSError(f"写向量失败：{last}")

        first = next(iter(vectors.values()), None)
        meta = {
            "sig": self.sig,
            "dim": len(first) if first else self.dim,
            "count": len(vectors),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}Z",
        }
        tmp2 = f"{self.meta_path}.tmp-{os.getpid()}"
        with open(tmp2, "w", encoding="utf-8", newline="\n") as f:
            json.dump(meta, f, indent=1)
        for _ in range(6):
            try:
                os.replace(tmp2, self.meta_path)
                break
            except OSError:
                time.sleep(0.03)
        self._cache = vectors
        try:
            st = os.stat(self.vec_path)
            self._cache_sig = f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            self._cache_sig = ""
        return True

    # ---------- 增删查 ----------

    def missing_ids(self, items: list[dict], limit: int = 300) -> list[dict]:
        have = self.load()
        out = []
        for it in items:
            i = it.get("id")
            if not i or i in have:
                continue
            if not str(it.get("text") or "").strip():
                continue
            out.append(it)
            if len(out) >= limit:
                break
        return out

    def remember(self, item_id: str, vec: list[float]) -> None:
        """只改内存，等 flush 统一落盘（整理时一批一批补，别每次都全量重写）。"""
        if not item_id or not vec:
            return
        ok, _why = self.writable()
        if not ok:
            return  # 不可写就别改内存，否则调用方会以为写成功了
        self.load()[item_id] = [float(x) for x in vec]
        self._dirty = True

    def forget(self, item_id: str) -> None:
        ok, _why = self.writable()
        if not ok:
            return
        mem = self.load()
        if item_id in mem:
            del mem[item_id]
            self._dirty = True

    def get(self, item_id: str) -> list[float] | None:
        return self.load().get(item_id)

    def flush(self) -> int:
        """把内存里的改动落盘。返回写入条数（没改动返回 0）。"""
        if not self._dirty:
            return 0
        ok, why = self.writable()
        if not ok:
            return 0
        mem = self.load()
        if not self.save(dict(mem)):
            return 0
        self._dirty = False
        return len(mem)

    def put(self, pairs: dict[str, list[float]]) -> int:
        if not pairs:
            return 0
        for k, v in pairs.items():
            if v:
                self.remember(k, v)
        self.flush()
        return len(pairs)

    def search(self, query_vec: list[float], k: int = 6, min_sim: float = 0.0) -> list[tuple[str, float]]:
        vectors = self.load()
        scored = []
        for i, v in vectors.items():
            s = cosine(query_vec, v)
            if s >= min_sim:
                scored.append((i, s))
        scored.sort(key=lambda x: -x[1])
        return scored[: max(1, k)]

    def stats(self) -> dict:
        m = self.meta()
        try:
            n = len(self.load())
        except OSError:
            n = 0
        ok, why = self.writable()
        return {
            "sig": self.sig,
            "meta_sig": m.get("sig", ""),
            "dim": m.get("dim", 0),
            "count": n,
            "writable": ok,
            "reason": why,
            "path": self.vec_path,
        }


__all__ = ["VectorStore", "cosine", "encode", "decode", "signature_of"]
