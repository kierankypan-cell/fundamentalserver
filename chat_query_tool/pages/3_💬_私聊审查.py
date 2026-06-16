"""
pages/3_🗨️_私聊审查.py —— 玩家私聊审查（点对点私聊回溯）

镜像 app.py / 收信审查 的查询模型，但聚焦"点对点私聊"：
  拉出该玩家在时间段内的所有私聊（双向：他发出的 + 别人发给他的），
  全部按国家主要语言翻译 + 风险评估。

交互形态不同：
  第一屏按"私聊对象"聚合（每个对象聊了多少句、谁发的多、有无违规）；
  点选某对象 → 弹出 st.dialog 模态框，把该对象的对话按时间升序渲染成
  类微信气泡（我方靠右绿、对方靠左白），被屏蔽句打红标记、违规句标注；
  关闭弹窗（X 或「返回列表」）即回到第一屏。

并发限流跨页面共享（db.MAX_CONCURRENT_USERS / try_acquire_slot）。
历史落盘到独立目录 chat_query_tool/history_private/。
session_state / widget key 全部加 private_ / priv_ 前缀，避免与其他页冲突。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from analyzer import analyze_out_battle
from db import (
    MAX_CONCURRENT_USERS,
    active_user_count,
    query_private_chats,
    release_slot,
    try_acquire_slot,
)
from prompts import COUNTRY_LANGUAGE
import history_private as hist
from private_helpers import (
    add_partner_cols,
    build_excel_private,
    enrich_private,
    partner_summary,
    render_conversation_html,
    _safe_int,
    _to_display_private,
)
from ui_helpers import _validate


# ============================================================
# 页面配置 + session_state
# ============================================================

st.set_page_config(
    page_title = "私聊审查",
    page_icon  = "🗨️",
    layout     = "wide",
)

if "private_history" not in st.session_state:
    st.session_state.private_history = hist.list_all()
if "private_view_id" not in st.session_state:
    st.session_state.private_view_id = None


# ============================================================
# 侧边栏：历史记录
# ============================================================

st.markdown(
    "<style>[data-testid='stSidebarNav']{display:none;}</style>",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.page_link("app.py",                       label="发言审查", icon="💬")
    st.page_link("pages/2_💔_收信审查.py",       label="收信审查", icon="💔")
    st.page_link("pages/3_💬_私聊审查.py",       label="私聊审查", icon="🗨️")
    st.divider()

    st.header("📜 私聊查询历史")
    st.caption(f"最多保留 {hist.MAX_HISTORY} 条 · 落盘到 chat_query_tool/history_private/")

    if not st.session_state.private_history:
        st.info("（暂无历史记录）")
    else:
        for h in st.session_state.private_history:
            ts       = h.get("ts", "")[:19].replace("T", " ")
            country  = h.get("country") or "?"
            partners = h.get("partner_count", 0)
            msgs     = h.get("msg_count", 0)
            label    = (f"{ts}\n{h['roleid']} · zone {h['zoneid']} · {country} · "
                        f"{partners} 个对象 / {msgs} 句")
            active   = h["id"] == st.session_state.private_view_id
            if st.button(
                label,
                key                 = f"priv_hist_{h['id']}",
                use_container_width = True,
                type                = "primary" if active else "secondary",
            ):
                st.session_state.private_view_id = h["id"]
                st.rerun()

        st.markdown("---")
        if st.button("🗑️ 清空所有私聊历史", key="priv_clear_all",
                     use_container_width=True):
            hist.clear_all()
            st.session_state.private_history = []
            st.session_state.private_view_id = None
            st.rerun()


# ============================================================
# 主区：标题 + 输入表单
# ============================================================

st.title("🗨️ 玩家私聊审查工具")
st.caption("拉出该玩家与每个对象的全部点对点私聊，按主要语言翻译；点对象看类微信对话气泡")

mode = st.radio("时长模式", ["预设时长", "自定义日期"],
                horizontal=True, key="priv_mode")

with st.form("priv_query_form"):
    col1, col2 = st.columns(2)
    with col1:
        roleid_str = st.text_input("玩家 roleid", placeholder="例如 8677905",
                                   key="priv_roleid")
    with col2:
        zoneid_str = st.text_input("zoneid", placeholder="例如 4001",
                                   key="priv_zoneid")

    if mode == "预设时长":
        days = st.selectbox(
            "时长",
            options     = [1, 3, 7, 14, 30],
            index       = 2,
            format_func = lambda d: f"近 {d} 天",
            key         = "priv_days",
        )
        end_d   = date.today()
        start_d = end_d - timedelta(days=days)
    else:
        ca, cb = st.columns(2)
        with ca:
            start_d = st.date_input("开始日期",
                                    value=date.today() - timedelta(days=7),
                                    key="priv_start_d")
        with cb:
            end_d   = st.date_input("结束日期",
                                    value=date.today(),
                                    key="priv_end_d")

    st.caption("ℹ️ 私聊只发生在战斗外，且跨区常见——按 roleid 拉取双向私聊，不按 zoneid 过滤；"
               "zoneid 仅用于检测国家。")

    submitted = st.form_submit_button("🔍 查询",
                                      type="primary", use_container_width=True)


# ============================================================
# 弹窗：单个对象的微信式对话
# ============================================================

@st.dialog("🗨️ 私聊详情", width="large")
def _conversation_dialog(df_partner: pd.DataFrame, roleid: int, partner_id: int):
    n      = len(df_partner)
    n_send = int((df_partner["sender"].astype("int64") == int(roleid)).sum())
    n_recv = n - n_send
    risk   = (df_partner["risk_level"].map(_safe_int)
              if "risk_level" in df_partner.columns else pd.Series([], dtype=int))
    shield = (df_partner["is_shield"].map(_safe_int)
              if "is_shield" in df_partner.columns else pd.Series([], dtype=int))
    n_risk   = int((risk >= 1).sum()) if len(risk) else 0
    n_shield = int((shield == 1).sum()) if len(shield) else 0

    st.markdown(f"#### 与玩家 `{partner_id}` 的私聊")
    st.caption(
        f"共 {n} 句（我方发出 {n_send} · 对方发来 {n_recv}） · "
        f"违规 {n_risk} · 已屏蔽 {n_shield} · 我方靠右绿、对方靠左白")

    st.markdown(render_conversation_html(df_partner, roleid),
                unsafe_allow_html=True)

    if st.button("⬅️ 返回列表", key=f"priv_back_{partner_id}",
                 use_container_width=True):
        st.rerun()


# ============================================================
# 渲染：整页结果（第一屏 + 触发弹窗）
# ============================================================

def _render_private_results(meta: dict, df: pd.DataFrame):
    country   = meta.get("country") or "?"
    lang_hint = COUNTRY_LANGUAGE.get((country or "").upper(), "未知")
    ts        = meta.get("ts", "")[:19].replace("T", " ")
    roleid    = int(meta["roleid"])

    st.markdown(
        f"### 玩家 `{meta['roleid']}` (zone `{meta['zoneid']}`) · "
        f"国家 `{country}`（主要语言：{lang_hint}） · "
        f"`{meta['start']}` → `{meta['end']}`"
    )
    st.caption(f"查询时间：{ts}")

    if df is None or df.empty:
        st.warning("该时间段内没有任何私聊记录。")
        return

    work = add_partner_cols(df, roleid)
    risk   = work["risk_level"].map(_safe_int) if "risk_level" in work.columns else pd.Series(0, index=work.index)
    shield = work["is_shield"].map(_safe_int) if "is_shield" in work.columns else pd.Series(0, index=work.index)
    n_partners = int(work["partner"].nunique())

    # ── 顶部小结 ──────────────────────────────────────────
    st.markdown("---")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("私聊总条数", len(work))
    c2.metric("私聊对象数", n_partners)
    c3.metric("我方发出",   int((work["direction"] == "send").sum()))
    c4.metric("违规(≥1)",   int((risk >= 1).sum()))
    c5.metric("已屏蔽",     int((shield == 1).sum()))

    # ── 导出 ──────────────────────────────────────────────
    ce1, ce2 = st.columns(2)
    with ce1:
        excel_bytes = build_excel_private(df, roleid)
        st.download_button(
            label     = "📦 导出完整私聊报告 (Excel)",
            data      = excel_bytes,
            file_name = (f"chat_private_{meta['roleid']}_{meta['zoneid']}"
                         f"_{meta['start']}_{meta['end']}.xlsx"),
            mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type      = "primary",
            key       = f"priv_xlsx_{meta['id']}",
            use_container_width = True,
        )
    with ce2:
        csv_bytes = _to_display_private(df, roleid).to_csv(
            index=False).encode("utf-8-sig")
        st.download_button(
            label     = "📥 导出全部明细 CSV",
            data      = csv_bytes,
            file_name = (f"chat_private_{meta['roleid']}_{meta['zoneid']}"
                         f"_{meta['start']}_{meta['end']}.csv"),
            mime      = "text/csv",
            key       = f"priv_csv_{meta['id']}",
            use_container_width = True,
        )

    # ── 第一屏：按私聊对象聚合 ─────────────────────────────
    st.markdown("---")
    st.subheader("👥 按私聊对象")
    summary = partner_summary(df, roleid)
    st.dataframe(summary, use_container_width=True, hide_index=True)

    st.markdown("**点选对象查看类微信对话气泡：**")
    # 每行渲染一个对象按钮；点击即在该 True 分支内打开弹窗
    # （弹窗只在按钮返回 True 的那次 rerun 中被调用，X / 返回触发 rerun 后即关闭）
    for _, r in summary.iterrows():
        partner_id = int(r["私聊对象"])
        label = (f"对象 {partner_id} · {r['消息总数']} 句"
                 f"（我方 {r['我方发送']} / 对方 {r['对方发送']}）"
                 f" · 违规 {r['含违规(≥1)']} · 已屏蔽 {r['已屏蔽']}")
        if st.button(label, key=f"priv_open_{meta['id']}_{partner_id}",
                     use_container_width=True):
            df_partner = work[work["partner"] == partner_id]
            _conversation_dialog(df_partner, roleid, partner_id)


# ============================================================
# 主流程：表单提交 → 跑查询 → 落盘 → 重渲染
# ============================================================

if submitted:
    roleid, zoneid = _validate(roleid_str, zoneid_str, start_d, end_d)
    start_ymd = start_d.strftime("%Y-%m-%d")
    end_ymd   = end_d.strftime("%Y-%m-%d")

    df = pd.DataFrame()
    country: str | None = None

    with st.status("准备查询...", expanded=True) as status:
        ahead = active_user_count()
        if ahead >= MAX_CONCURRENT_USERS:
            status.update(label=f"⏳ 当前有 {ahead} 人正在查询，排队中...")

        waited = 0
        acquired = False
        while not acquired:
            acquired = try_acquire_slot(timeout=2)
            if not acquired:
                waited += 2
                ahead = active_user_count()
                status.update(label=f"⏳ 前方还有 {ahead} 人在查询，已等 {waited}s...")

        try:
            status.update(label="并发执行：geoip 检测 + 双向私聊...")
            country, df = query_private_chats(roleid, zoneid, start_ymd, end_ymd)

            if country:
                lang = COUNTRY_LANGUAGE.get(country, "未在常见列表内")
                st.write(f"📍 国家 `{country}`(主要语言：{lang})")
            else:
                st.write("📍 ⚠️ 未找到登录记录或 IP 无法定位，翻译时不附加语言提示")

            st.write(f"📥 私聊 {len(df)} 条")
            status.update(label="数据库查询完成 ✓", state="complete")
        except Exception as e:
            status.update(label="数据库查询失败 ✗", state="error")
            st.error(f"数据库查询失败：{e}")
            st.stop()
        finally:
            release_slot()

    if df.empty:
        st.warning("⚠️ 该玩家在所选时间段内没有任何私聊记录。"
                   "建议加长时间窗口或检查 roleid / zoneid。")
        st.stop()

    st.write("**私聊内容** 翻译 + 风险分析中...")
    bar = st.progress(0.0, text="准备中")
    try:
        df = enrich_private(
            df, analyze_out_battle,
            progress_callback = lambda cur, total: bar.progress(
                cur / total, text=f"批次 {cur}/{total}"),
            country = country,
        )
        bar.empty()
    except Exception as e:
        bar.empty()
        st.error(f"私聊分析失败：{e}")
        st.stop()

    qid = hist.save(roleid, zoneid, country, start_ymd, end_ymd, df)
    st.session_state.private_history = hist.list_all()
    st.session_state.private_view_id = qid
    st.rerun()


# ============================================================
# 渲染：基于 view_id 从磁盘读
# ============================================================

if st.session_state.private_view_id:
    try:
        meta, df = hist.load(st.session_state.private_view_id)
    except Exception as e:
        st.error(f"加载历史记录失败：{e}")
        st.session_state.private_view_id = None
    else:
        _render_private_results(meta, df)
else:
    st.info("👆 输入 roleid + zoneid 查询，或在左侧边栏点选历史记录回看")
