"""
history_private.py —— 私聊审查查询历史落盘 + 检索

与 history.py / history_received.py 平行，区别两处：
    1. HISTORY_DIR = chat_query_tool/history_private/
    2. 私聊只有一个场景（战斗外点对点），因此只落一张 DataFrame：chats.csv
       meta.json 字段：id, ts, roleid, zoneid, country, start, end,
                        msg_count（私聊总条数）, partner_count（私聊对象数）

每条记录目录：history_private/{qid}/
    meta.json    元数据
    chats.csv    双向私聊 enriched DataFrame（含 translation / risk_*）
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

HISTORY_DIR = Path(__file__).parent / "history_private"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
MAX_HISTORY = 20


def _qid(roleid: int, zoneid: int) -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(roleid)}_{int(zoneid)}"


def _partner_count(df: pd.DataFrame, roleid: int) -> int:
    """统计私聊对象数：每行非 roleid 的一方（发出取 target，收到取 sender）去重。"""
    if df is None or df.empty:
        return 0
    if not {"sender", "target"}.issubset(df.columns):
        return 0
    sender = df["sender"].astype("int64")
    target = df["target"].astype("int64")
    partners = target.where(sender == int(roleid), sender)
    return int(partners.nunique())


def save(roleid: int, zoneid: int, country: str | None,
         start_ymd: str, end_ymd: str,
         df: pd.DataFrame) -> str:
    """落盘一条私聊审查查询，返回 qid。"""
    qid = _qid(roleid, zoneid)
    folder = HISTORY_DIR / qid
    folder.mkdir(parents=True, exist_ok=True)

    meta = {
        "id":            qid,
        "ts":            datetime.now().isoformat(timespec="seconds"),
        "roleid":        int(roleid),
        "zoneid":        int(zoneid),
        "country":       country,
        "start":         start_ymd,
        "end":           end_ymd,
        "msg_count":     int(len(df)) if df is not None else 0,
        "partner_count": _partner_count(df, roleid),
    }
    (folder / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if df is not None and not df.empty:
        df.to_csv(folder / "chats.csv", index=False, encoding="utf-8-sig")

    _prune()
    return qid


def list_all() -> list[dict]:
    """按时间倒序列出所有有效历史记录的 meta（最新在前）。"""
    if not HISTORY_DIR.exists():
        return []
    out: list[dict] = []
    for d in sorted(HISTORY_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            out.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def load(qid: str) -> tuple[dict, pd.DataFrame]:
    """根据 qid 读取元数据 + DataFrame；csv 不存在时返回空 DataFrame。"""
    folder = HISTORY_DIR / qid
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))

    chats_path = folder / "chats.csv"
    df = (pd.read_csv(chats_path, encoding="utf-8-sig")
          if chats_path.exists() else pd.DataFrame())
    return meta, df


def delete(qid: str) -> None:
    folder = HISTORY_DIR / qid
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)


def clear_all() -> None:
    if not HISTORY_DIR.exists():
        return
    for d in HISTORY_DIR.iterdir():
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


def _prune() -> None:
    """超过 MAX_HISTORY 时删最老的目录。"""
    valid = [
        d for d in HISTORY_DIR.iterdir()
        if d.is_dir() and (d / "meta.json").exists()
    ]
    valid.sort(reverse=True)
    for d in valid[MAX_HISTORY:]:
        shutil.rmtree(d, ignore_errors=True)
