"""从 livingmemory 的真实数据生成回放数据集。

数据源（只读）：astrbot 里 livingmemory 插件的 SQLite
  documents      —— 每条是一次「记忆」（群聊/私聊的整理结果）
  memory_atoms   —— 原子事实，用来抽人名
补充构造几组，专门覆盖机制边界：重复 / 矛盾 / 进展 / 长文 / 很旧的流水账 / 多场景。

输出：<run>/dataset.json
用法：python astrbot/test/live/dataset.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(HERE, "run")

DB = (
    r"C:/Users/Anxiple/.workbuddy/workspace/sessions/"
    r"23c4edef-c339-481d-83f8-2b5f2e381fec/modify_backup/2.m.8fbd149b.livingmemory.db"
)

# 太多条会让摘要/判关系调用量上去；只取前 N 条真实记忆
MAX_REAL = 60


def ms(dt_str: str) -> int:
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(dt_str, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return 1762000000000


def scene_of(meta: dict) -> str:
    """把 livingmemory 的 `session_id` 还原成注入用的 scene。

    原始格式是 ``群名:消息类型:群号``（如 ``大肥鱼:GroupMessage:966657516``）。
    ★ 取的是第 0 段（群名），不是第 1 段——第 1 段是消息类型，
      早先写成 ``split(":")[1:2]``，结果全变成 ``qq·GroupMessage``，
      等于把"哪个群"的信息丢了，按 scene 分组和过滤都退化成一个大桶。
    """
    parts = [x for x in str(meta.get("session_id") or "").split(":") if x]
    kind = parts[1] if len(parts) > 1 else ""
    if kind == "FriendMessage":
        return "qq·私聊"
    name = parts[0] if parts else ""
    return f"qq·{name}" if name else "qq·未知"


def load_real() -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()
    rows = []
    for text, meta_raw, created in cur.execute(
        "select text, metadata, created_at from documents order by id limit ?", (MAX_REAL,)
    ):
        try:
            meta = json.loads(meta_raw or "{}")
        except json.JSONDecodeError:
            meta = {}
        name = str(meta.get("persona_id") or "").strip()
        rows.append({
            "text": str(text or "").strip()[:600],
            "speaker": "user",
            "scene": scene_of(meta),
            "people": [name] if name else [],
            "createdAt": ms(str(created)),
            "importance": float(meta.get("importance") or 0.5),
            "tag": "real",
        })
    con.close()
    return [r for r in rows if r["text"]]


NOW = 1762000000000
DAY = 86400000


def build_extra() -> list[dict]:
    """专门覆盖机制边界的补充数据（不是真实聊天，但贴近真实表达）。"""
    base = dict(speaker="user", scene="qq·项目群", people=["小鱼"], importance=0.6)

    def mk(text, **kw):
        d = dict(base)
        d.update(kw)
        d["text"] = text
        return d

    return [
        # —— 重复：同一件事换个说法，考 FUSE_DUP ——
        mk("老王说这周五之前必须把报告交上去，别再拖了", tag="dup-a", createdAt=NOW - 2 * DAY),
        mk("周五要交报告，老王又提醒了一遍，说不能再拖", tag="dup-b", createdAt=NOW - 1 * DAY),

        # —— 矛盾：前后两个决定，考 SUPERSEDE ——
        mk("定下来了：以后所有报告统一用 Word 格式提交", tag="contra-a", createdAt=NOW - 5 * DAY),
        mk("算了，报告还是用 markdown 吧，Word 排版太麻烦", tag="contra-b", createdAt=NOW - 3 * DAY),

        # —— 进展：同一件事的不同阶段，考 FUSE_UPDATE ——
        mk("nnUNet 训练到第 3 轮，验证集 dice 只有 0.72，还得调", tag="update-a", createdAt=NOW - 4 * DAY),
        mk("nnUNet 训练到第 10 轮，验证集 dice 到 0.88 了，这次稳了", tag="update-b", createdAt=NOW - 1 * DAY),

        # —— 长文：一段技术讨论，考摘要压缩与截断 ——
        mk(
            "关于肺结节分割的后处理，我们把连通域小于 30 个体素的预测块全删了，原因是"
            "这批 CT 里小结节占了 37 个，太小的连通域基本都是血管断面造成的假阳性。"
            "另外 12 例的标注方向是反的，已经单独列了清单交给标注同学返工。"
            "评估口径统一按召回率优先，因为临床上漏诊的代价远大于误诊；"
            "在这个前提下再看 dice。缓存这块也改了：层文件按 mtime 失效，"
            "十万条检索从 1.4 秒降到 91 毫秒。下周把推理服务封成 docker，"
            "挂到内网给放射科先试用两周，收集一轮反馈再决定要不要上 GPU 集群。",
            tag="long", createdAt=NOW - 6 * DAY,
        ),

        # —— 很旧的流水账：考 DL1 遗忘（60 天前，保持率远低于 0.25）——
        *[
            mk(f"两个月前的一条流水账：第{i}天在群里闲聊了几句，没什么要紧的",
               tag="old", createdAt=NOW - (60 + i) * DAY, importance=0.4)
            for i in range(5)
        ],

        # —— 多场景：私聊 / 另一个群，考分组与 scene 过滤 ——
        mk("私聊里说：下周三去医院复查，记得带上次的 CT 片子",
           tag="scene", scene="qq·私聊", people=["小鱼"], createdAt=NOW - 2 * DAY),
        mk("另一个群里在讨论显卡价格，说 4090 又涨了",
           tag="scene", scene="qq·硬件群", people=["阿凯"], createdAt=NOW - 2 * DAY),
    ]


def main() -> None:
    os.makedirs(RUN_DIR, exist_ok=True)
    real = load_real()
    extra = build_extra()
    data = real + extra
    out = os.path.join(RUN_DIR, "dataset.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    scenes = {}
    for r in data:
        scenes[r["scene"]] = scenes.get(r["scene"], 0) + 1
    chars = sum(len(r["text"]) for r in data)
    print(f"OK {out}")
    print(f"  真实 {len(real)} 条 + 构造 {len(extra)} 条 = {len(data)} 条，{chars} 字")
    print(f"  场景：{scenes}")
    print(f"  tag：{ {t: sum(1 for r in data if r['tag'] == t) for t in {r['tag'] for r in data} } }")


if __name__ == "__main__":
    main()
