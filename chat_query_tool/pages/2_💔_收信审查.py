"""
pages/2_💔_收信审查.py —— 玩家收信审查（被骚扰查询）

镜像 app.py 的查询模型：把"该玩家发了什么"换成"别人发给/对着他说了什么"。

战斗内：先在 battle_end 找该玩家参与过的所有 battle_id（哪怕全程沉默也覆盖），
        再回 chat_talk 拉这些局的整局聊天（含本人 + 队友/对手发言）；
        UI 把本人发言行用浅蓝底色区分，AI 风险只对他人发言做。
战斗外：私聊收件（target = roleid 且 sender != roleid）。

并发限流跨页面共享（与主页共用 db.MAX_CONCURRENT_USERS / try_acquire_slot）。
历史落盘到独立目录 chat_query_tool/history_received/。
session_state / widget key 全部加 recv_ 前缀，避免与主页冲突。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from analyzer import analyze_in_battle, analyze_out_battle
from db import (
    MAX_CONCURRENT_USERS,
    active_user_count,
    query_received_chats,
    release_slot,
    try_acquire_slot,
)
from prompts import COUNTRY_LANGUAGE
import history_received as hist
from received_helpers import (
    enrich_with_others_only,
    received_stats,
    build_excel_received,
    _to_display_received_in,
    _to_display_received_out,
    _highlight_in_battle_received,
    _summary_received,
)
from ui_helpers import (
    _validate,
    _highlight_risk,
    _overview_table,
    _shield_table,
    _breakdown,
)


# ============================================================
# 页面配置 + session_state
# ============================================================

st.set_page_config(
    page_title = "收信审查",
    page_icon  = "💔",
    layout     = "wide",
)

if "received_history" not in st.session_state:
    st.session_state.received_history = hist.list_all()
if "received_view_id" not in st.session_state:
    st.session_state.received_view_id = None


# ============================================================
# 侧边栏：历史记录
# ============================================================

# 隐藏 streamlit 自动从 pages/ 目录推断出的侧边栏导航；用 st.page_link 自己渲染
st.markdown(
    "<style>[data-testid='stSidebarNav']{display:none;}</style>",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.page_link("app.py",                       label="发言审查", icon="💬")
    st.page_link("pages/2_💔_收信审查.py",       label="收信审查", icon="💔")
    st.divider()

    st.header("📜 收信查询历史")
    st.caption(f"最多保留 {hist.MAX_HISTORY} 条 · 落盘到 chat_query_tool/history_received/")

    if not st.session_state.received_history:
        st.info("（暂无历史记录）")
    else:
        for h in st.session_state.received_history:
            ts      = h.get("ts", "")[:19].replace("T", " ")
            country = h.get("country") or "?"
            others  = h.get("in_others_count", 0)
            recv    = h.get("out_received_count", 0)
            label   = (f"{ts}\n{h['roleid']} · zone {h['zoneid']} · {country} · "
                       f"内他人 {others} / 外收 {recv}")
            active  = h["id"] == st.session_state.received_view_id
            if st.button(
                label,
                key                 = f"recv_hist_{h['id']}",
                use_container_width = True,
                type                = "primary" if active else "secondary",
            ):
                st.session_state.received_view_id = h["id"]
                st.rerun()

        st.markdown("---")
        if st.button("🗑️ 清空所有收信历史", key="recv_clear_all",
                     use_container_width=True):
            hist.clear_all()
            st.session_state.received_history = []
            st.session_state.received_view_id = None
            st.rerun()


# ============================================================
# 主区：标题 + 输入表单
# ============================================================

st.title("💔 玩家收信审查工具")
st.caption("查『被人骂』——别人对该玩家说了什么、是否含违规内容、屏蔽是否兜住")

mode = st.radio("时长模式", ["预设时长", "自定义日期"],
                horizontal=True, key="recv_mode")

with st.form("recv_query_form"):
    col1, col2 = st.columns(2)
    with col1:
        roleid_str = st.text_input("玩家 roleid", placeholder="例如 8677905",
                                   key="recv_roleid")
    with col2:
        zoneid_str = st.text_input("zoneid", placeholder="例如 4001",
                                   key="recv_zoneid")

    if mode == "预设时长":
        days = st.selectbox(
            "时长",
            options     = [1, 3, 7, 14, 30],
            index       = 2,
            format_func = lambda d: f"近 {d} 天",
            key         = "recv_days",
        )
        end_d   = date.today()
        start_d = end_d - timedelta(days=days)
    else:
        ca, cb = st.columns(2)
        with ca:
            start_d = st.date_input("开始日期",
                                    value=date.today() - timedelta(days=7),
                                    key="recv_start_d")
        with cb:
            end_d   = st.date_input("结束日期",
                                    value=date.today(),
                                    key="recv_end_d")

    submitted = st.form_submit_button("🔍 查询",
                                      type="primary", use_container_width=True)


# ============================================================
# 渲染：战斗内 Tab（整局聊天 + 本人/他人区分 + 风险高亮）
# ============================================================

def _render_in_battle_received(df: pd.DataFrame, meta: dict):
    if df.empty:
        st.info("该时间段内没有【战斗内】聊天记录")
        return

    s = received_stats(df)

    # ── 一句话结论 ────────────────────────────────────────
    if s["others_count"] == 0:
        st.info(f"📭 该玩家有 {s['self_count']} 条本人发言，但其他玩家在这些局都没说话")
    elif s["n_risky"] == 0:
        st.success(f"✅ 共 {s['others_count']} 条他人发言，全部正常")
    elif s["visible_risky"] == 0:
        st.warning(
            f"⚠️ 检测到他人发言中 {s['n_risky']} 条违规内容，"
            f"但**全部被屏蔽**，玩家应该没看到")
    else:
        st.error(
            f"🚨 他人发言中检测到 {s['n_risky']} 条违规（严重 {s['n_severe']} 条），"
            f"其中 **{s['visible_risky']} 条未被屏蔽** — 玩家可能看到了")

    # ── 总览 + 屏蔽性能（沿用主页表格组件，分母是他人发言）─────
    cl, cr = st.columns(2)
    with cl:
        st.markdown("**📊 总览**（分母：他人发言）")
        st.dataframe(_overview_table(s),
                     use_container_width=True, hide_index=True)
    with cr:
        st.markdown("**🛡️ 屏蔽系统性能**（仅他人发言参与统计）")
        st.dataframe(_shield_table(s),
                     use_container_width=True, hide_index=True)

    # ── 明细分布（仅基于他人发言）──────────────────────────
    self_mask = df["risk_level"].astype(int) == -1
    others    = df[~self_mask]
    if not others.empty:
        st.markdown("**🔬 他人发言风险明细分布**")
        by_label = st.radio(
            "分组维度",
            options          = ["按风险等级", "按风险类型"],
            horizontal       = True,
            label_visibility = "collapsed",
            key              = "recv_breakdown_in",
        )
        by_col = "risk_level" if by_label == "按风险等级" else "risk_type"
        st.dataframe(_breakdown(others, by_col),
                     use_container_width=True, hide_index=True)

    # ── 整局聊天明细（本人浅蓝 / 他人按风险高亮）──────────
    st.markdown("**📝 整局聊天明细**（按 battle_id 分组、time 升序）")
    display_df = _to_display_received_in(df, int(meta["roleid"]))
    st.dataframe(
        display_df.style.apply(_highlight_in_battle_received, axis=1),
        use_container_width = True,
        height              = 480,
    )

    csv_bytes = display_df.to_csv(index=False).encode("utf-8-sig")
    file_stem = (f"chat_received_{meta['roleid']}_{meta['zoneid']}"
                 f"_{meta['start']}_{meta['end']}_in")
    st.download_button(
        label     = "📥 导出【战斗内整局】CSV",
        data      = csv_bytes,
        file_name = f"{file_stem}.csv",
        mime      = "text/csv",
        key       = f"recv_dl_{file_stem}",
    )


# ============================================================
# 渲染：战斗外 Tab（私聊收件，仅他人发给我）
# ============================================================

def _render_out_battle_received(df: pd.DataFrame, meta: dict):
    if df.empty:
        st.info("该时间段内没有【战斗外】私聊收件")
        return

    # 战斗外 SQL 已过滤 sender != roleid，所有行都送过 AI；走 received_stats 即可
    s = received_stats(df)

    if s["n_risky"] == 0:
        st.success(f"✅ 共收到 {s['others_count']} 条私聊，未发现违规内容")
    elif s["visible_risky"] == 0:
        st.warning(
            f"⚠️ 收到 {s['n_risky']} 条违规私聊，但**全部被屏蔽**，玩家应该没看到")
    else:
        st.error(
            f"🚨 收到 {s['n_risky']} 条违规私聊（严重 {s['n_severe']} 条），"
            f"其中 **{s['visible_risky']} 条未被屏蔽** — 玩家可能看到了")

    cl, cr = st.columns(2)
    with cl:
        st.markdown("**📊 总览**")
        st.dataframe(_overview_table(s),
                     use_container_width=True, hide_index=True)
    with cr:
        st.markdown("**🛡️ 屏蔽系统性能**")
        st.dataframe(_shield_table(s),
                     use_container_width=True, hide_index=True)

    st.markdown("**🔬 风险明细分布**")
    by_label = st.radio(
        "分组维度",
        options          = ["按风险等级", "按风险类型"],
        horizontal       = True,
        label_visibility = "collapsed",
        key              = "recv_breakdown_out",
    )
    by_col = "risk_level" if by_label == "按风险等级" else "risk_type"
    st.dataframe(_breakdown(df, by_col),
                 use_container_width=True, hide_index=True)

    st.markdown("**📝 私聊收件明细**")
    display_df = _to_display_received_out(df)
    st.dataframe(
        display_df.style.apply(_highlight_risk, axis=1),
        use_container_width = True,
        height              = 480,
    )

    csv_bytes = display_df.to_csv(index=False).encode("utf-8-sig")
    file_stem = (f"chat_received_{meta['roleid']}_{meta['zoneid']}"
                 f"_{meta['start']}_{meta['end']}_out")
    st.download_button(
        label     = "📥 导出【战斗外收件】CSV",
        data      = csv_bytes,
        file_name = f"{file_stem}.csv",
        mime      = "text/csv",
        key       = f"recv_dl_{file_stem}",
    )


# ============================================================
# 渲染：顶层（meta + 总览 + Excel + 双 Tab）
# ============================================================

def _render_received_results(meta: dict, df_in: pd.DataFrame, df_out: pd.DataFrame):
    country   = meta.get("country") or "?"
    lang_hint = COUNTRY_LANGUAGE.get((country or "").upper(), "未知")
    ts        = meta.get("ts", "")[:19].replace("T", " ")

    st.markdown(
        f"### 玩家 `{meta['roleid']}` (zone `{meta['zoneid']}`) · "
        f"国家 `{country}`(主要语言:{lang_hint}) · "
        f"`{meta['start']}` → `{meta['end']}`"
    )
    st.caption(f"查询时间：{ts}")

    if df_in.empty and df_out.empty:
        st.warning("该时间段内没有任何收信记录。")
        return

    st.markdown("---")
    st.subheader("📋 总览结论")
    st.markdown(
        f"- {_summary_received(df_in,  '**战斗内**')}\n"
        f"- {_summary_received(df_out, '**战斗外**')}"
    )

    excel_bytes = build_excel_received(df_in, df_out, int(meta["roleid"]))
    st.download_button(
        label     = "📦 导出完整收信报告 (Excel · 双 Sheet)",
        data      = excel_bytes,
        file_name = (f"chat_received_{meta['roleid']}_{meta['zoneid']}"
                     f"_{meta['start']}_{meta['end']}.xlsx"),
        mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type      = "primary",
        key       = f"recv_xlsx_{meta['id']}",
    )

    st.markdown("---")
    tab_in, tab_out = st.tabs(["⚔️ 战斗内整局", "🌐 战斗外私聊收件"])
    with tab_in:
        _render_in_battle_received(df_in, meta)
    with tab_out:
        _render_out_battle_received(df_out, meta)


# ============================================================
# 主流程：表单提交 → 跑查询 → 落盘 → 重渲染
# ============================================================

if submitted:
    roleid, zoneid = _validate(roleid_str, zoneid_str, start_d, end_d)
    start_ymd = start_d.strftime("%Y-%m-%d")
    end_ymd   = end_d.strftime("%Y-%m-%d")

    df_in  = pd.DataFrame()
    df_out = pd.DataFrame()
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
            status.update(label="并发执行：geoip 检测 + 战斗内整局 + 战斗外收件...")
            country, df_out, df_in = query_received_chats(
                roleid, zoneid, start_ymd, end_ymd
            )

            if country:
                lang = COUNTRY_LANGUAGE.get(country, "未在常见列表内")
                st.write(f"📍 国家 `{country}`(主要语言：{lang})")
            else:
                st.write("📍 ⚠️ 未找到登录记录或 IP 无法定位，翻译时不附加语言提示")

            st.write(f"📥 战斗外收件 {len(df_out)} 条 / 战斗内整局 {len(df_in)} 条")
            status.update(label="数据库查询完成 ✓", state="complete")
        except Exception as e:
            status.update(label="数据库查询失败 ✗", state="error")
            st.error(f"数据库查询失败：{e}")
            st.stop()
        finally:
            release_slot()

    if df_in.empty and df_out.empty:
        st.warning("⚠️ 该玩家在所选时间段内没有任何聊天 / 战斗记录。"
                   "建议加长时间窗口或检查 roleid / zoneid。")
        st.stop()

    if not df_out.empty:
        st.write("**战斗外收件** 内容分析中...")
        bar = st.progress(0.0, text="准备中")
        try:
            df_out = enrich_with_others_only(
                df_out, roleid, analyze_out_battle,
                progress_callback = lambda cur, total: bar.progress(
                    cur / total, text=f"批次 {cur}/{total}"),
                country = country,
            )
            bar.empty()
        except Exception as e:
            bar.empty()
            st.error(f"战斗外分析失败：{e}")
            st.stop()

    if not df_in.empty:
        st.write("**战斗内整局** 内容分析中（仅他人发言送审）...")
        bar = st.progress(0.0, text="准备中")
        try:
            df_in = enrich_with_others_only(
                df_in, roleid, analyze_in_battle,
                progress_callback = lambda cur, total: bar.progress(
                    cur / total, text=f"批次 {cur}/{total}"),
                country = country,
            )
            bar.empty()
        except Exception as e:
            bar.empty()
            st.error(f"战斗内分析失败：{e}")
            st.stop()

    qid = hist.save(roleid, zoneid, country, start_ymd, end_ymd, df_in, df_out)
    st.session_state.received_history = hist.list_all()
    st.session_state.received_view_id = qid
    st.rerun()


# ============================================================
# 渲染：基于 view_id 从磁盘读
# ============================================================

if st.session_state.received_view_id:
    try:
        meta, df_in, df_out = hist.load(st.session_state.received_view_id)
    except Exception as e:
        st.error(f"加载历史记录失败：{e}")
        st.session_state.received_view_id = None
    else:
        _render_received_results(meta, df_in, df_out)
else:
    st.info("👆 输入 roleid + zoneid 查询，或在左侧边栏点选历史记录回看")
