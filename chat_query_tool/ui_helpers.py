"""
ui_helpers.py —— UI 渲染辅助纯函数

从 app.py 抽出，主页（app.py）和收信审查页（pages/2_*.py）共用。
模块仅包含函数定义，**不执行任何顶层 streamlit 渲染**——
否则其他页面 import 时会污染各自的页面布局。

包含：
    _validate          —— 表单输入校验（用 st.error/st.stop 反馈错误）
    _highlight_risk    —— 风险等级行底色（红/橙/黄）
    _to_display        —— DataFrame 列重命名 + 排序，统一展示格式
    _summary           —— 一句话场景结论（违规数 / 严重数 / 未屏蔽数）
    _fmt_pct           —— 整数比例 → 百分比字符串
    _overview_table    —— 总览表（消息数 / 违规率 / 已屏蔽 ...）
    _shield_table      —— 屏蔽召回 / 准确率 + 混淆矩阵
    _breakdown         —— 按风险等级 / 风险类型 分组明细
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st


# ============================================================
# 表单校验
# ============================================================

def _validate(roleid_str: str, zoneid_str: str, start_d: date, end_d: date):
    if not roleid_str.strip() or not zoneid_str.strip():
        st.error("请填写 roleid 和 zoneid")
        st.stop()
    try:
        roleid = int(roleid_str.strip())
        zoneid = int(zoneid_str.strip())
    except ValueError:
        st.error("roleid 和 zoneid 必须为整数")
        st.stop()
    if start_d > end_d:
        st.error("开始日期必须早于或等于结束日期")
        st.stop()
    return roleid, zoneid


# ============================================================
# 风险高亮
# ============================================================

def _highlight_risk(row):
    level = int(row.get("风险等级", 0))
    if level >= 3: return ["background-color: #ffcccc"] * len(row)
    if level == 2: return ["background-color: #ffe0b3"] * len(row)
    if level == 1: return ["background-color: #fff5cc"] * len(row)
    return [""] * len(row)


# ============================================================
# DataFrame 展示格式化
# ============================================================

def _to_display(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.rename(columns={
        "time":          "时间",
        "content":       "原文",
        "translation":   "翻译",
        "risk_level":    "风险等级",
        "risk_type":     "风险类型",
        "is_shield":     "是否被屏蔽",
        "risk_reason":   "风险原因",
    })
    cols = ["时间", "原文", "翻译", "风险等级", "风险类型", "是否被屏蔽",
            "风险原因"]
    cols = [c for c in cols if c in out.columns]
    out  = out[cols]
    return out.sort_values(["风险等级", "时间"], ascending=[False, True]).reset_index(drop=True)


# ============================================================
# 一句话结论
# ============================================================

def _summary(df: pd.DataFrame, scene_name: str) -> str:
    if df.empty:
        return f"{scene_name}：（无聊天记录）"
    total  = len(df)
    risky  = int((df["risk_level"] >= 1).sum())
    severe = int((df["risk_level"] >= 2).sum())
    visible_risky = int(((df["risk_level"] >= 1)
                         & (df["is_shield"].astype(int) == 0)).sum())
    if risky == 0:
        return f"{scene_name}：✅ 全部正常（共 {total} 条）"
    icon = "🚨" if severe > 0 else "⚠️"
    return (f"{scene_name}：{icon} 检测到 {risky} 条违规"
            f"（严重 {severe} 条，未被屏蔽 {visible_risky} 条）"
            f"／总 {total} 条")


# ============================================================
# 百分比格式化
# ============================================================

def _fmt_pct(num: int, den: int) -> str:
    """整数比例格式化为百分比；分母为 0 时返回 '-'。"""
    if den == 0:
        return "-"
    return f"{num / den * 100:.1f}%"


# ============================================================
# 总览 / 屏蔽性能 / 风险明细分布表
# ============================================================

def _overview_table(s: dict) -> pd.DataFrame:
    return pd.DataFrame([
        {"指标": "总消息数",       "值": str(s["total"])},
        {"指标": "违规消息（≥1）", "值": str(s["n_risky"])},
        {"指标": "严重违规（≥2）", "值": str(s["n_severe"])},
        {"指标": "违规率",         "值": _fmt_pct(s["n_risky"], s["total"])},
        {"指标": "已屏蔽数",       "值": str(s["n_shielded"])},
        {"指标": "违规且未屏蔽",   "值": str(s["visible_risky"])},
    ])


def _shield_table(s: dict) -> pd.DataFrame:
    """以 AI 判断为真值，屏蔽系统的混淆矩阵 + 召回 / 准确率。"""
    return pd.DataFrame([
        {"指标": "召回率（TP/(TP+FN)）",
         "值":   _fmt_pct(s["tp"], s["tp"] + s["fn"]),
         "说明": "违规中被屏蔽的比例（漏屏蔽得越少越高）"},
        {"指标": "准确率（TP/(TP+FP)）",
         "值":   _fmt_pct(s["tp"], s["tp"] + s["fp"]),
         "说明": "屏蔽里真违规的比例（误屏蔽得越少越高）"},
        {"指标": "真阳 TP", "值": str(s["tp"]), "说明": "违规 ∩ 屏蔽"},
        {"指标": "假阳 FP", "值": str(s["fp"]), "说明": "正常 ∩ 屏蔽（误屏蔽）"},
        {"指标": "假阴 FN", "值": str(s["fn"]), "说明": "违规 ∩ 未屏蔽（漏屏蔽）"},
        {"指标": "真阴 TN", "值": str(s["tn"]), "说明": "正常 ∩ 未屏蔽"},
    ])


def _breakdown(df: pd.DataFrame, by: str) -> pd.DataFrame:
    """
    按 by ('risk_level' / 'risk_type') 分组的明细。
    列：分组 / 消息数 / 占比 / 已屏蔽 / 屏蔽率
    """
    total = len(df)
    if by == "risk_level":
        col_name = "风险等级"
        labels   = {0: "0（正常）", 1: "1（轻微）", 2: "2（中度）", 3: "3（严重）"}
        keys     = [0, 1, 2, 3]
    else:
        col_name = "风险类型"
        keys     = list(df[by].value_counts().index)
        labels   = {k: str(k) for k in keys}

    rows = []
    for key in keys:
        sub      = df[df[by] == key]
        n        = len(sub)
        shielded = int((sub["is_shield"].astype(int) == 1).sum()) if n else 0
        rows.append({
            col_name: labels[key],
            "消息数": n,
            "占比":   _fmt_pct(n, total),
            "已屏蔽": shielded,
            "屏蔽率": _fmt_pct(shielded, n),
        })
    return pd.DataFrame(rows)
