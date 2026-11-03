"""硅基流动客户端（标准库，供回放脚本用）。

三个模型都用平台上的：
  embed  BAAI/bge-m3               免费
  rerank BAAI/bge-reranker-v2-m3   免费
  chat   deepseek-ai/DeepSeek-V4.1-Flash

注意：rerank 的返回字段是 `relevance_score`，这里统一映射成 `score`，
否则 tiers.search 会当成 0 分（真接平台时踩过一次）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = "https://api.siliconflow.cn/v1"
EMBED_MODEL = "BAAI/bge-m3"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
CHAT_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash"


class SiliconError(RuntimeError):
    pass


class Silicon:
    def __init__(self, key: str | None = None, base: str = BASE, timeout: float = 90.0):
        self.key = (key if key is not None else os.environ.get("SILICON_API_KEY", "")).strip()
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.calls = {"chat": 0, "embed": 0, "rerank": 0}
        self.tokens = {"prompt": 0, "completion": 0}
        self.seconds = 0.0

    def _post(self, path: str, payload: dict) -> dict:
        if not self.key:
            raise SiliconError("没有配置 SILICON_API_KEY")
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise SiliconError(f"HTTP {e.code}: {detail}") from e
        except Exception as e:
            raise SiliconError(f"{type(e).__name__}: {e}") from e
        finally:
            self.seconds += time.perf_counter() - t0
        return body

    # ---------- chat ----------

    def chat(self, system: str, user: str, max_tokens: int = 1200,
             temperature: float = 0.2, timeout: float | None = None) -> str:
        body = self._post("/chat/completions", {
            "model": CHAT_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "enable_thinking": False,
        })
        self.calls["chat"] += 1
        u = body.get("usage") or {}
        self.tokens["prompt"] += int(u.get("prompt_tokens") or 0)
        self.tokens["completion"] += int(u.get("completion_tokens") or 0)
        msg = (body.get("choices") or [{}])[0].get("message") or {}
        return str(msg.get("content") or "")

    # ---------- embed ----------

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = self._post("/embeddings", {
            "model": EMBED_MODEL,
            "input": [str(t)[:512] for t in texts],
        })
        self.calls["embed"] += 1
        data = body.get("data") or []
        out = [None] * len(texts)
        for i, row in enumerate(data):
            idx = int(row.get("index", i))
            if 0 <= idx < len(out):
                out[idx] = row.get("embedding")
        return [v if v is not None else [] for v in out]

    # ---------- rerank ----------

    def rerank(self, query: str, docs: list[str], top_n: int = 0) -> list[dict]:
        if not docs:
            return []
        payload = {
            "model": RERANK_MODEL,
            "query": str(query or ""),
            "documents": [str(d)[:300] for d in docs],
        }
        if top_n > 0:
            payload["top_n"] = min(top_n, len(docs))
        body = self._post("/rerank", payload)
        self.calls["rerank"] += 1
        return [
            {"index": int(x.get("index") or 0),
             "score": float(x.get("relevance_score") or x.get("score") or 0)}
            for x in (body.get("results") or [])
        ]

    def stats(self) -> dict:
        return {"calls": dict(self.calls), "tokens": dict(self.tokens),
                "api_seconds": round(self.seconds, 1)}
