"""
received_helpers.py —— 收信审查特有辅助：AI 结果回填、统计、Excel 导出

与主页 app.py 的差异：
  战斗内拉的是"整局聊天"（含本人 + 9 位他人），AI 风险只对 sender != roleid 的他人发言做；
  本人发言行用 risk_level=-1 标记，统计时排除（分母 = 他人发言数）。
  战斗外 SQL 已过滤 sender != roleid，所有行都送审。
"""

from __future__ import annotations

import io
from typing import Callable, Optional

import pandas as pd


# ============================================================
# AI 结果回填（只分析他人发言）
# ============================================================

def enrich_with_others_only(
    df: pd.DataFrame,
    roleid: int,
    analyzer_fn: Callable[..., list[dict]],
    progress_callback: Optional[Callable[[int, int], None]] = None,
    country: Optional[str] = None,
) -> pd.DataFrame:
    """
    对 sender != roleid 的行送 AI 分析；本人发言行用 risk_level=-1 标记不送审。

    新增列：translation / risk_level / risk_type / risk_reason
    其中 risk_level=-1 / risk_type='self' 表示该行是本人发言（统计时排除）。
    """
    if df.empty:
        return df.copy()

    df = df.reset_index(drop=True).copy()
    others_idx = df.index[df["sender"].astype("int64") != int(roleid)].tolist()

    df["translation"] = ""
    df["risk_level"]  = -1
    df["risk_type"]   = "self"
    df["risk_reason"] = "本人发言，未做风险分析"

    if not others_idx:
        return df

    msgs = df.loc[others_idx, "content"].fillna("").astype(str).tolist()
    results = analyzer_fn(msgs, progress_callback=progress_callback, country=country)

    for local_i, global_i in enumerate(others_idx):
        r = results[local_i]
        df.at[global_i, "translation"] = r["translation"]
        df.at[global_i, "risk_level"]  = r["risk_level"]
        df.at[global_i, "risk_type"]   = r["risk_type"]
        df.at[global_i, "risk_reason"] = r["risk_reason"]
    return df


# ============================================================
# 收信场景统计（分母 = 他人发言数）
# ============================================================

def received_stats(df: pd.DataFrame) -> dict:
    """
    收信场景的违规率与屏蔽混淆矩阵——分母为他人发言（risk_level != -1）。

    返回字段与主页 _stats 兼容（total / n_risky / n_severe / n_shielded /
    visible_risky / tp / fp / fn / tn），可直接喂给 _overview_table /
    _shield_table；额外多 self_count / others_count 给 UI 顶部显示。
    """
    if df.empty:
        return dict(total=0, n_risky=0, n_severe=0, n_shielded=0,
                    visible_risky=0, tp=0, fp=0, fn=0, tn=0,
                    self_count=0, others_count=0)

    self_mask = df["risk_level"].astype(int) == -1
    others    = df[~self_mask]

    if others.empty:
        return dict(total=0, n_risky=0, n_severe=0, n_shielded=0,
                    visible_risky=0, tp=0, fp=0, fn=0, tn=0,
                    self_count=int(self_mask.sum()), others_count=0)

    risky    = others["risk_level"].astype(int) >= 1
    severe   = others["risk_level"].astype(int) >= 2
    shielded = others["is_shield"].astype(int) == 1

    tp = int(( risky &  shielded).sum())
    fp = int((~risky &  shielded).sum())
    fn = int(( risky & ~shielded).sum())
    tn = int((~risky & ~shielded).sum())

    return dict(
        total         = len(others),
        n_risky       = int(risky.sum()),
        n_severe      = int(severe.sum()),
        n_shielded    = int(shielded.sum()),
        visible_risky = fn,
        tp=tp, fp=fp, fn=fn, tn=tn,
        self_count   = int(self_mask.sum()),
        others_count = len(others),
    )


# ============================================================
# 收信场景 DataFrame 展示格式化
# ============================================================

def _to_display_received_in(df: pd.DataFrame, roleid: int) -> pd.DataFrame:
    """
    战斗内整局展示：列前置 battle_id / sender / 是否本人；
    其余字段沿用主页风格。排序：battle_id ASC, time ASC（同局聊天连续显示）。
    """
    if df.empty:
        return df

    out = df.rename(columns={
        "time":          "时间",
        "battle_id":     "battle_id",
        "sender":        "sender",
        "target":        "target",
        "content":       "原文",
        "translation":   "翻译",
        "risk_level":    "风险等级",
        "risk_type":     "风险类型",
        "is_shield":     "是否被屏蔽",
        "risk_reason":   "风险原因",
    }).copy()
    out["是否本人"] = (out["sender"].astype("int64") == int(roleid)).map(
        lambda b: "本人" if b else "他人"
    )

    cols = ["battle_id", "时间", "sender", "是否本人", "target", "原文", "翻译",
            "风险等级", "风险类型", "是否被屏蔽", "风险原因"]
    cols = [c for c in cols if c in out.columns]
    out  = out[cols]
    return out.sort_values(["battle_id", "时间"],
                           ascending=[True, True]).reset_index(drop=True)


def _to_display_received_out(df: pd.DataFrame) -> pd.DataFrame:
    """战斗外私聊收件展示：列前置 sender；其余沿用主页风格、按风险等级降序。"""
    if df.empty:
        return df

    out = df.rename(columns={
        "time":          "时间",
        "sender":        "sender",
        "target":        "target",
        "content":       "原文",
        "translation":   "翻译",
        "risk_level":    "风险等级",
        "risk_type":     "风险类型",
        "is_shield":     "是否被屏蔽",
        "risk_reason":   "风险原因",
    })
    cols = ["时间", "sender", "原文", "翻译", "风险等级", "风险类型", "是否被屏蔽",
            "风险原因"]
    cols = [c for c in cols if c in out.columns]
    out  = out[cols]
    return out.sort_values(["风险等级", "时间"],
                           ascending=[False, True]).reset_index(drop=True)


# ============================================================
# 风险高亮（战斗内：本人浅蓝；他人按风险红/橙/黄）
# ============================================================

def _highlight_in_battle_received(row):
    """战斗内行底色：本人发言浅蓝，他人发言按风险等级红/橙/黄/无。"""
    if row.get("是否本人") == "本人":
        return ["background-color: #e6f0ff"] * len(row)
    level = int(row.get("风险等级", 0))
    if level >= 3: return ["background-color: #ffcccc"] * len(row)
    if level == 2: return ["background-color: #ffe0b3"] * len(row)
    if level == 1: return ["background-color: #fff5cc"] * len(row)
    return [""] * len(row)


# ============================================================
# 收信场景一句话结论（分母为他人发言）
# ============================================================

def _summary_received(df: pd.DataFrame, scene_name: str) -> str:
    if df.empty:
        return f"{scene_name}：（无聊天记录）"

    s = received_stats(df)
    if s["others_count"] == 0:
        return f"{scene_name}：（仅本人 {s['self_count']} 条发言，无他人发言）"

    if s["n_risky"] == 0:
        return f"{scene_name}：✅ 他人发言全部正常（共 {s['others_count']} 条他人发言）"

    icon = "🚨" if s["n_severe"] > 0 else "⚠️"
    return (f"{scene_name}：{icon} 他人发言中检测到 {s['n_risky']} 条违规"
            f"（严重 {s['n_severe']} 条，未被屏蔽 {s['visible_risky']} 条）"
            f"／他人共 {s['others_count']} 条")


# ============================================================
# Excel 导出（双 Sheet：战斗内整局 / 战斗外收件）
# ============================================================

def build_excel_received(df_in: pd.DataFrame,
                         df_out: pd.DataFrame,
                         roleid: int,
                         scope: str = "all") -> bytes:
    """scope 决定空 sheet 的占位文案：被本次查询排除时显示「未查询」而非「无记录」。"""
    in_empty_hint  = ("（本次未查询该场景）" if scope == "out"
                      else "该时间段内无战斗内聊天")
    out_empty_hint = ("（本次未查询该场景）" if scope == "in"
                      else "该时间段内无战斗外收件")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        if not df_in.empty:
            _to_display_received_in(df_in, roleid).to_excel(
                writer, sheet_name="战斗内整局", index=False)
        else:
            pd.DataFrame([{"提示": in_empty_hint}]).to_excel(
                writer, sheet_name="战斗内整局", index=False)

        if not df_out.empty:
            _to_display_received_out(df_out).to_excel(
                writer, sheet_name="战斗外收件", index=False)
        else:
            pd.DataFrame([{"提示": out_empty_hint}]).to_excel(
                writer, sheet_name="战斗外收件", index=False)
    buf.seek(0)
    return buf.getvalue()
