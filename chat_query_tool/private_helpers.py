"""
private_helpers.py —— 私聊审查特有辅助：AI 回填、私聊对象聚合、微信式气泡渲染、导出

私聊只有战斗外一个场景（gameserver_chat_talk_v2 点对点），双向都拉：
  我方发出（sender==roleid）+ 对方发来（target==roleid）。
所有行都送 AI 翻译 + 风险评估（私聊属社交场景，复用战斗外提示词最贴切）。

第一屏按「私聊对象」聚合（每个对象聊了多少句）；点选某对象弹窗，
把该对象的对话按时间升序渲染成类微信气泡（我方靠右绿、对方靠左白），
被屏蔽的句子打红标记、违规句标注 risk_reason。
"""

from __future__ import annotations

import html as _html
import io
import math
from typing import Callable, Optional

import pandas as pd


# ============================================================
# 安全数值转换（CSV 回读后字段可能是 str / float / NaN）
# ============================================================

def _safe_int(val, default: int = 0) -> int:
    try:
        if val is None:
            return default
        if isinstance(val, float) and math.isnan(val):
            return default
        s = str(val).strip()
        if s == "" or s.lower() == "nan":
            return default
        return int(float(s))
    except (ValueError, TypeError):
        return default


# ============================================================
# AI 结果回填（全部行送审）
# ============================================================

def enrich_private(
    df: pd.DataFrame,
    analyzer_fn: Callable[..., list[dict]],
    progress_callback: Optional[Callable[[int, int], None]] = None,
    country: Optional[str] = None,
) -> pd.DataFrame:
    """
    对全部行的 content 送 AI 翻译 + 风险评估，回填四列：
        translation / risk_level / risk_type / risk_reason
    """
    if df is None or df.empty:
        return df.copy() if df is not None else df

    df = df.reset_index(drop=True).copy()
    msgs = df["content"].fillna("").astype(str).tolist()
    results = analyzer_fn(msgs, progress_callback=progress_callback, country=country)

    df["translation"] = [r["translation"] for r in results]
    df["risk_level"]  = [r["risk_level"]  for r in results]
    df["risk_type"]   = [r["risk_type"]   for r in results]
    df["risk_reason"] = [r["risk_reason"] for r in results]
    return df


# ============================================================
# 私聊对象 / 方向派生列
# ============================================================

def add_partner_cols(df: pd.DataFrame, roleid: int) -> pd.DataFrame:
    """
    加两列：
      partner   —— 私聊对象（行内非 roleid 的一方：发出取 target，收到取 sender）
      direction —— "send"（我方发出）/ "recv"（对方发来）
    """
    if df is None or df.empty:
        return df.copy() if df is not None else df

    out = df.reset_index(drop=True).copy()
    sender = out["sender"].astype("int64")
    target = out["target"].astype("int64")
    is_self = sender == int(roleid)
    out["partner"]   = target.where(is_self, sender).astype("int64")
    out["direction"] = is_self.map(lambda b: "send" if b else "recv")
    return out


# ============================================================
# 私聊对象聚合（第一屏）
# ============================================================

def partner_summary(df: pd.DataFrame, roleid: int) -> pd.DataFrame:
    """
    按私聊对象聚合，返回展示用 DataFrame，按消息总数降序。
    列：私聊对象 / 消息总数 / 我方发送 / 对方发送 / 含违规(≥1) / 已屏蔽 / 最近时间
    """
    if df is None or df.empty:
        return pd.DataFrame()

    work = add_partner_cols(df, roleid)
    risk_s   = (work["risk_level"].map(_safe_int) if "risk_level" in work.columns
                else pd.Series(0, index=work.index))
    shield_s = (work["is_shield"].map(_safe_int) if "is_shield" in work.columns
                else pd.Series(0, index=work.index))
    work = work.assign(_risky=risk_s >= 1, _shielded=shield_s == 1)

    rows = []
    for partner, g in work.groupby("partner"):
        rows.append({
            "私聊对象":  int(partner),
            "消息总数":  len(g),
            "我方发送":  int((g["direction"] == "send").sum()),
            "对方发送":  int((g["direction"] == "recv").sum()),
            "含违规(≥1)": int(g["_risky"].sum()),
            "已屏蔽":    int(g["_shielded"].sum()),
            "最近时间":  str(g["time"].max()),
        })
    out = pd.DataFrame(rows)
    return out.sort_values("消息总数", ascending=False).reset_index(drop=True)


# ============================================================
# 完整明细表（CSV / Excel 导出）
# ============================================================

def _to_display_private(df: pd.DataFrame, roleid: int) -> pd.DataFrame:
    """明细展示：前置 私聊对象 / 方向；其余沿用主页列命名。按对象、时间排序。"""
    if df is None or df.empty:
        return df.copy() if df is not None else df

    work = add_partner_cols(df, roleid)
    out = work.rename(columns={
        "time":          "时间",
        "partner":       "私聊对象",
        "sender":        "sender",
        "target":        "target",
        "content":       "原文",
        "translation":   "翻译",
        "risk_level":    "风险等级",
        "risk_type":     "风险类型",
        "is_shield":     "是否被屏蔽",
        "risk_reason":   "风险原因",
    }).copy()
    out["方向"] = out["direction"].map({"send": "我方发出", "recv": "对方发来"})

    cols = ["私聊对象", "方向", "时间", "原文", "翻译", "风险等级", "风险类型",
            "是否被屏蔽", "风险原因", "sender", "target"]
    cols = [c for c in cols if c in out.columns]
    out  = out[cols]
    return out.sort_values(["私聊对象", "时间"],
                           ascending=[True, True]).reset_index(drop=True)


def build_excel_private(df: pd.DataFrame, roleid: int) -> bytes:
    """单 Sheet「私聊」导出。"""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        if df is not None and not df.empty:
            _to_display_private(df, roleid).to_excel(
                writer, sheet_name="私聊", index=False)
        else:
            pd.DataFrame([{"提示": "该时间段内无私聊记录"}]).to_excel(
                writer, sheet_name="私聊", index=False)
    buf.seek(0)
    return buf.getvalue()


# ============================================================
# 类微信气泡渲染（弹窗内）
# ============================================================

_RISK_BORDER = {1: "#e6c200", 2: "#e67e00", 3: "#d63030"}  # 黄 / 橙 / 红


def _bubble(content: str, translation: str, time_str: str,
            is_self: bool, shielded: bool, level: int,
            risk_type: str, risk_reason: str) -> str:
    """单条气泡 HTML。我方靠右绿、对方靠左白；屏蔽/违规打标记。"""
    align   = "flex-end" if is_self else "flex-start"
    bg      = "#95ec69" if is_self else "#ffffff"
    border  = (f"2px solid {_RISK_BORDER[level]}" if level in _RISK_BORDER
               else "1px solid #e3e3e3")

    badges = []
    if shielded:
        badges.append(
            '<span style="display:inline-block;background:#fde2e2;color:#c00;'
            'font-size:11px;padding:1px 6px;border-radius:8px;margin-right:6px;">'
            '🚫 已屏蔽</span>')
    if level >= 1:
        rr = _html.escape(risk_reason or "")
        rt = _html.escape(risk_type or "")
        badges.append(
            f'<span style="display:inline-block;background:#fff3cd;color:#8a6d00;'
            f'font-size:11px;padding:1px 6px;border-radius:8px;" '
            f'title="{rr}">⚠️ L{level} {rt}</span>')
    badges_html = (f'<div style="margin-top:4px;">{"".join(badges)}</div>'
                   if badges else "")

    trans_html = ""
    if translation and translation.strip():
        trans_html = (
            '<div style="margin-top:5px;padding-top:5px;'
            'border-top:1px dashed #c8c8c8;color:#666;font-size:12.5px;">'
            f'{_html.escape(translation)}</div>')

    reason_html = ""
    if level >= 1 and risk_reason and risk_reason.strip():
        reason_html = (
            f'<div style="margin-top:3px;color:#b06a00;font-size:11px;">'
            f'· {_html.escape(risk_reason)}</div>')

    return (
        f'<div style="display:flex;justify-content:{align};margin:2px 0;">'
        f'<div style="max-width:82%;">'
        f'<div style="font-size:10.5px;color:#999;margin:0 4px 2px;'
        f'text-align:{"right" if is_self else "left"};">{_html.escape(time_str)}</div>'
        f'<div style="background:{bg};border:{border};border-radius:8px;'
        f'padding:8px 11px;font-size:14px;color:#111;line-height:1.45;'
        f'word-break:break-word;white-space:pre-wrap;">'
        f'{_html.escape(content)}'
        f'{trans_html}{badges_html}{reason_html}'
        f'</div></div></div>'
    )


def render_conversation_html(df_partner: pd.DataFrame, roleid: int) -> str:
    """整段对话气泡 HTML（按 time 升序，外层可滚动容器）。"""
    if df_partner is None or df_partner.empty:
        return '<div style="padding:16px;color:#888;">（无对话内容）</div>'

    rows = df_partner.sort_values("time", ascending=True)
    bubbles = []
    for _, r in rows.iterrows():
        is_self = _safe_int(r.get("sender")) == int(roleid)
        bubbles.append(_bubble(
            content     = str(r.get("content") or ""),
            translation = str(r.get("translation") or ""),
            time_str    = str(r.get("time") or ""),
            is_self     = is_self,
            shielded    = _safe_int(r.get("is_shield")) == 1,
            level       = _safe_int(r.get("risk_level")),
            risk_type   = str(r.get("risk_type") or ""),
            risk_reason = str(r.get("risk_reason") or ""),
        ))

    return (
        '<div style="display:flex;flex-direction:column;gap:6px;padding:14px;'
        'background:#ededed;border-radius:8px;max-height:76vh;overflow-y:auto;">'
        + "".join(bubbles) +
        '</div>'
    )
