"""
history.py —— 查询历史落盘 + 检索（account_risk）

每条记录是一个目录：history/{qid}/
    meta.json    元数据（id, ts, roleid, zoneid, start, end, login_count, rebind_count）
    login.csv    登录原始 DataFrame（含 geo_city/geo_isp，UTF-8-BOM）
    rebind.csv   切换账号原始 DataFrame（UTF-8-BOM）

DataFrame 为空时不写对应 csv。最多保留 MAX_HISTORY 条，超出删最老的。
回看时重新跑校验/折叠（逻辑在 timeline.py），因此只需存原始两表即可。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

HISTORY_DIR = Path(__file__).parent / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
MAX_HISTORY = 20


def _qid(roleid: int, zoneid: int) -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(roleid)}_{int(zoneid)}"


def save(roleid: int, zoneid: int, start_ymd: str, end_ymd: str,
         df_login: pd.DataFrame, df_rebind: pd.DataFrame,
         create_device_id: str | None = None,
         create_ymd: str | None = None) -> str:
    """落盘一条查询，返回 qid。df_rebind 为自创角以来的全量 rebind（含窗口外）。"""
    qid = _qid(roleid, zoneid)
    folder = HISTORY_DIR / qid
    folder.mkdir(parents=True, exist_ok=True)

    meta = {
        "id":               qid,
        "ts":               datetime.now().isoformat(timespec="seconds"),
        "roleid":           int(roleid),
        "zoneid":           int(zoneid),
        "start":            start_ymd,
        "end":              end_ymd,
        "create_device_id": create_device_id,
        "create_ymd":       create_ymd,
        "login_count":      int(len(df_login))  if df_login  is not None else 0,
        "rebind_count":     int(len(df_rebind)) if df_rebind is not None else 0,
    }
    (folder / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if df_login is not None and not df_login.empty:
        df_login.to_csv(folder / "login.csv", index=False, encoding="utf-8-sig")
    if df_rebind is not None and not df_rebind.empty:
        df_rebind.to_csv(folder / "rebind.csv", index=False, encoding="utf-8-sig")

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
    """读取元数据 + 登录/切换账号两个 DataFrame；csv 不存在时返回空 DataFrame。"""
    folder = HISTORY_DIR / qid
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))

    login_path  = folder / "login.csv"
    rebind_path = folder / "rebind.csv"

    df_login = (pd.read_csv(login_path, encoding="utf-8-sig", dtype=str)
                if login_path.exists() else pd.DataFrame())
    df_rebind = (pd.read_csv(rebind_path, encoding="utf-8-sig", dtype=str)
                 if rebind_path.exists() else pd.DataFrame())
    return meta, df_login, df_rebind


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
