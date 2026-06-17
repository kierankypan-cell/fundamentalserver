"""
timeline.py —— 异常登录检测 + 合并登录/切换账号(rebind)时间线 + 折叠可见性

三步：
  annotate_logins(df_login, df_rebind_full, create_device_id, validate_fn)
      给每条登录补：设备指纹校验(4.1–4.5) + 异常登录(req4) 的原因与红旗标记
  build_timeline(df_login_annotated, df_rebind_inwindow)
      合并为按 time 升序的统一时间线（rebind 只取查询窗口内的，避免数年记录刷屏）
  compute_folding(merged)  →  (visible_mask, blocks)
      连续相同 (deviceid,device) 登录折叠中间、只留首尾；红旗/rebind/首尾为锚点

异常登录(req4)判定：以「创角设备 + 切换账号」解释每台登录设备的来源。
  设备 D 的登录正常，当且仅当：
    D == 创角设备 deviceid；或
    存在 device_id==D 的 rebind 记录且其时间 ≤ 该登录时间（账号在此前已切到该设备）。
  否则 → 异常登录（设备凭空出现、登录前无对应切号）→ 标红。
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

MIN_FOLD = 2   # 连续隐藏 < 该值时不折叠，直接显示（折 1 条无意义）

ABNORMAL_TAG = "🚩 异常登录"


# ============================================================
# 异常登录检测 + 登录标注
# ============================================================

def _first_bind_per_device(df_rebind_full: pd.DataFrame) -> dict[str, pd.Timestamp]:
    """
    登录设备(login.deviceid) -> 该设备最早一次被切入(rebind)的时间。

    匹配键是 rebind 的 **account_name**（= 被绑定的账号名，对设备账号而言其值即 login.deviceid），
    而非 rebind.device_id。
    """
    if (df_rebind_full is None or df_rebind_full.empty
            or "account_name" not in df_rebind_full.columns):
        return {}
    rb = df_rebind_full.copy()
    rb["_t"] = pd.to_datetime(rb["time"], errors="coerce")
    rb = rb.dropna(subset=["_t"])
    rb["account_name"] = rb["account_name"].astype(str)
    return rb.groupby("account_name")["_t"].min().to_dict()


def annotate_logins(df_login: pd.DataFrame,
                    df_rebind_full: pd.DataFrame,
                    create_device_id: str | None,
                    validate_fn: Callable[[dict], list[str]]) -> tuple[pd.DataFrame, dict]:
    """
    给登录表补充校验/异常列，返回 (annotated_df, info)。
    新增列：
      _fp_reasons  设备指纹校验失败原因（'；'拼接）
      _abn_reason  异常登录原因（'' 表示正常）
      _reasons     合并后的全部原因（红旗在前）
      _valid_fail  bool —— 指纹失败 或 异常登录
    info：{create_device_id, create_device_fallback(bool), n_fp_fail, n_abnormal}
    """
    out = df_login.copy()
    info = {"create_device_id": create_device_id, "create_device_fallback": False,
            "n_fp_fail": 0, "n_abnormal": 0}
    if out.empty:
        for c in ("_fp_reasons", "_abn_reason", "_reasons"):
            out[c] = pd.Series(dtype=str)
        out["_valid_fail"] = pd.Series(dtype=bool)
        return out, info

    # 创角设备未知（创角日登录数据已过期）→ 降级：用本数据集中最早登录的 deviceid 作基线
    eff_create = create_device_id
    if not eff_create and "deviceid" in out.columns:
        ot = out.copy()
        ot["_t"] = pd.to_datetime(ot["time"], errors="coerce")
        ot = ot.sort_values("_t")
        if not ot.empty:
            eff_create = str(ot.iloc[0].get("deviceid", "")) or None
            info["create_device_fallback"] = eff_create is not None
    info["create_device_id"] = eff_create

    first_bind = _first_bind_per_device(df_rebind_full)

    fp_list, abn_list = [], []
    for r in out.to_dict("records"):
        fp = validate_fn(r)
        fp_list.append("；".join(fp))

        d  = str(r.get("deviceid", "")).strip()
        t  = pd.to_datetime(r.get("time"), errors="coerce")
        abn = ""
        if d and not (eff_create and d == eff_create):
            fb = first_bind.get(d)
            explained = (fb is not None and pd.notna(t) and fb <= t)
            if not explained:
                if fb is None:
                    abn = f"{ABNORMAL_TAG}：设备非创角设备，且登录前无对应切换账号(rebind)记录"
                else:
                    abn = (f"{ABNORMAL_TAG}：登录时间早于该设备的切换账号时间"
                           f"（切号于 {fb:%Y/%m/%d %H:%M:%S}）")
        abn_list.append(abn)

    out["_fp_reasons"] = fp_list
    out["_abn_reason"] = abn_list
    out["_reasons"] = [
        "；".join(x for x in (a, f) if x) for a, f in zip(abn_list, fp_list)
    ]
    out["_valid_fail"] = [bool(a) or bool(f) for a, f in zip(abn_list, fp_list)]

    info["n_fp_fail"]   = int(sum(1 for f in fp_list if f))
    info["n_abnormal"]  = int(sum(1 for a in abn_list if a))
    return out, info


# ============================================================
# 合并时间线
# ============================================================

def build_timeline(df_login: pd.DataFrame,
                   df_rebind: pd.DataFrame) -> pd.DataFrame:
    """
    合并已标注的登录表 + 窗口内 rebind 为按时间升序的统一时间线。
    df_login 须已经过 annotate_logins（含 _reasons / _valid_fail）。
    """
    parts = []

    if df_login is not None and not df_login.empty:
        lg = df_login.copy()
        lg["_type"] = "login"
        if "_reasons" not in lg.columns:
            lg["_reasons"] = ""
        if "_valid_fail" not in lg.columns:
            lg["_valid_fail"] = False
        lg["_gkey"] = list(zip(lg.get("deviceid", "").astype(str),
                               lg.get("device", "").astype(str)))
        parts.append(lg)

    if df_rebind is not None and not df_rebind.empty:
        rb = df_rebind.copy()
        rb["_type"] = "rebind"
        rb["_reasons"]    = ""
        rb["_valid_fail"] = False
        rb["_gkey"]       = None
        parts.append(rb)

    if not parts:
        return pd.DataFrame()

    merged = pd.concat(parts, ignore_index=True, sort=False)
    merged["_time_dt"] = pd.to_datetime(merged["time"], errors="coerce")
    # 稳定排序：时间相同时 rebind 排在 login 前（理论上先切号、后登录）
    merged["_type_order"] = (merged["_type"] == "login").astype(int)
    merged = (merged.sort_values(["_time_dt", "_type_order"], kind="stable")
                    .drop(columns="_type_order")
                    .reset_index(drop=True))
    return merged


# ============================================================
# 折叠可见性
# ============================================================

def compute_folding(merged: pd.DataFrame) -> tuple[list[bool], list[dict]]:
    """
    返回 (visible_mask, blocks)。
      visible_mask[i] —— 第 i 行是否默认可见
      blocks          —— [{start, end, count, deviceid, device}]，每块为连续隐藏区间
    """
    n = len(merged)
    if n == 0:
        return [], []

    types = merged["_type"].tolist()
    fails = merged["_valid_fail"].tolist()
    gkeys = merged["_gkey"].tolist()

    visible = [False] * n

    # 1) 连续登录 run（同 _gkey、不被 rebind/异键打断），标记首尾
    i = 0
    while i < n:
        if types[i] != "login":
            i += 1
            continue
        j = i
        while j + 1 < n and types[j + 1] == "login" and gkeys[j + 1] == gkeys[i]:
            j += 1
        visible[i] = True
        visible[j] = True
        i = j + 1

    # 2) 强制锚点：rebind / 红旗；并带出紧邻上下条作为上下文
    def _mark_with_neighbors(idx: int):
        visible[idx] = True
        if idx - 1 >= 0:
            visible[idx - 1] = True
        if idx + 1 < n:
            visible[idx + 1] = True

    for idx in range(n):
        if types[idx] == "rebind" or fails[idx]:
            _mark_with_neighbors(idx)

    # 3) 整表首尾恒可见
    visible[0] = True
    visible[n - 1] = True

    # 4) 连续不可见 → 折叠块；单条直接显示
    blocks: list[dict] = []
    k = 0
    while k < n:
        if visible[k]:
            k += 1
            continue
        start = k
        while k < n and not visible[k]:
            k += 1
        end = k - 1
        count = end - start + 1
        if count < MIN_FOLD:
            for t in range(start, end + 1):
                visible[t] = True
            continue
        rep = merged.iloc[start]
        blocks.append({
            "start":    start,
            "end":      end,
            "count":    count,
            "deviceid": str(rep.get("deviceid", "")),
            "device":   str(rep.get("device", "")),
        })

    return visible, blocks
