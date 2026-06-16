"""
history_received.py —— 收信审查查询历史落盘 + 检索

与 history.py 完全平行（避免与发件历史目录串味），区别仅两处：
    1. HISTORY_DIR = chat_query_tool/history_received/
    2. meta.json 多两个字段：
        in_others_count    战斗内里他人发言数（risk_level != -1）
        out_received_count 战斗外私聊收件数

每条记录目录：history_received/{qid}/
    meta.json    元数据
    in.csv       战斗内整局 enriched DataFrame（含本人 + 他人）
    out.csv      战斗外私聊收件 enriched DataFrame
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

HISTORY_DIR = Path(__file__).parent / "history_received"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
MAX_HISTORY = 20


def _qid(roleid: int, zoneid: int) -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(roleid)}_{int(zoneid)}"


def save(roleid: int, zoneid: int, country: str | None,
         start_ymd: str, end_ymd: str,
         df_in: pd.DataFrame, df_out: pd.DataFrame,
         scope: str = "all") -> str:
    """落盘一条收信审查查询，返回 qid。scope 记录查询范围（all / in / out）。"""
    qid = _qid(roleid, zoneid)
    folder = HISTORY_DIR / qid
    folder.mkdir(parents=True, exist_ok=True)

    in_count = int(len(df_in)) if df_in is not None else 0
    out_count = int(len(df_out)) if df_out is not None else 0

    if df_in is not None and not df_in.empty and "risk_level" in df_in.columns:
        in_others = int((df_in["risk_level"].astype(int) != -1).sum())
    else:
        in_others = 0

    meta = {
        "id":                  qid,
        "ts":                  datetime.now().isoformat(timespec="seconds"),
        "roleid":              int(roleid),
        "zoneid":              int(zoneid),
        "country":             country,
        "start":               start_ymd,
        "end":                 end_ymd,
        "scope":               scope,
        "in_count":            in_count,
        "in_others_count":     in_others,
        "out_received_count":  out_count,
    }
    (folder / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if df_in is not None and not df_in.empty:
        df_in.to_csv(folder / "in.csv", index=False, encoding="utf-8-sig")
    if df_out is not None and not df_out.empty:
        df_out.to_csv(folder / "out.csv", index=False, encoding="utf-8-sig")

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


def load(qid: str) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """根据 qid 读取元数据 + 两个 DataFrame；csv 不存在时返回空 DataFrame。"""
    folder = HISTORY_DIR / qid
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))

    in_path  = folder / "in.csv"
    out_path = folder / "out.csv"

    df_in = (pd.read_csv(in_path, encoding="utf-8-sig")
             if in_path.exists() else pd.DataFrame())
    df_out = (pd.read_csv(out_path, encoding="utf-8-sig")
              if out_path.exists() else pd.DataFrame())
    return meta, df_in, df_out


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
