"""
app.py —— 玩家登录 / 账号风险查询工具（Streamlit 主入口）

启动方式：
    streamlit run account_risk/app.py

功能：
- 输入 roleid + zoneid + 时间范围，拉取该 roleid 的全部登录日志（含 geoip 城市 / 网络供应商）
- 对每条登录做设备指纹合法性校验（规则 4.1–4.5），失败行标红并列出原因
- 按 (deviceid, device) 折叠连续相同登录（只留首尾），支持全局 / 逐块展开
- 把「切换账号(rebind)」记录按时间插入登录时间线（蓝色行），另设页签看全部切换账号
- 左侧边栏保留最多 20 条历史，点选可重看
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import history
from db import (
    MAX_CONCURRENT_USERS,
    active_user_count,
    query_aux,
    query_login,
    release_slot,
    try_acquire_slot,
)
from timeline import annotate_logins, build_timeline, compute_folding
from ui_helpers import (
    _validate,
    build_rebind_display,
    render_timeline,
)
from validators import validate_login


def _derive_create_ymd(df_login: pd.DataFrame) -> str | None:
    """从登录数据里取创角日（usercreatetime 已denormalize到每行，与 create_role 一致）。"""
    if df_login is None or df_login.empty:
        return None
    for col in ("usercreatetime", "usercreateymd"):
        if col in df_login.columns:
            ts = pd.to_datetime(df_login[col], errors="coerce").dropna()
            if not ts.empty:
                return ts.min().strftime("%Y-%m-%d")
    return None


def _rebind_in_window(df_rebind: pd.DataFrame, start_ymd: str, end_ymd: str) -> pd.DataFrame:
    """从全量 rebind 中筛出查询窗口内的（用于时间线内联展示）。"""
    if df_rebind is None or df_rebind.empty:
        return pd.DataFrame()
    t = pd.to_datetime(df_rebind["time"], errors="coerce")
    lo = pd.Timestamp(start_ymd)
    hi = pd.Timestamp(end_ymd) + pd.Timedelta(days=1)
    return df_rebind[(t >= lo) & (t < hi)].reset_index(drop=True)


# ============================================================
# 页面配置 + session_state
# ============================================================

st.set_page_config(
    page_title = "玩家登录/账号风险查询",
    page_icon  = "🛡️",
    layout     = "wide",
)

if "history" not in st.session_state:
    st.session_state.history = history.list_all()
if "view_id" not in st.session_state:
    st.session_state.view_id = None


# ============================================================
# 侧边栏：历史记录
# ============================================================

with st.sidebar:
    st.header("📜 查询历史")
    st.caption(f"最多保留 {history.MAX_HISTORY} 条 · 落盘到 account_risk/history/")

    if not st.session_state.history:
        st.info("（暂无历史记录）")
    else:
        for h in st.session_state.history:
            ts     = h.get("ts", "")[:19].replace("T", " ")
            counts = f"登录 {h.get('login_count', 0)} / 切号 {h.get('rebind_count', 0)}"
            label  = f"{ts}\n{h['roleid']} · zone {h['zoneid']} · {counts}"
            active = h["id"] == st.session_state.view_id
            if st.button(
                label,
                key                 = f"hist_{h['id']}",
                use_container_width = True,
                type                = "primary" if active else "secondary",
            ):
                st.session_state.view_id = h["id"]
                st.rerun()

        st.markdown("---")
        if st.button("🗑️ 清空所有历史", use_container_width=True):
            history.clear_all()
            st.session_state.history = []
            st.session_state.view_id = None
            st.rerun()


# ============================================================
# 主区：标题 + 输入表单
# ============================================================

st.title("🛡️ 玩家登录 / 账号风险查询工具")
st.caption("输入玩家 roleid + zoneid + 时间范围，拉取登录日志 + geoip，"
           "做设备指纹校验，并把切换账号记录按时间插入时间线，辅助判断盗号风险")

# 时长模式放到 form 外，切换才能即时 rerun 出现日期框
mode = st.radio("时长模式", ["预设时长", "自定义日期"], horizontal=True, key="mode")

with st.form("query_form"):
    col1, col2 = st.columns(2)
    with col1:
        roleid_str = st.text_input("玩家 roleid", placeholder="例如 8677905")
    with col2:
        zoneid_str = st.text_input("zoneid", placeholder="例如 4001")

    if mode == "预设时长":
        days = st.selectbox(
            "时长",
            options     = [1, 3, 7, 14, 30],
            index       = 2,
            format_func = lambda d: f"近 {d} 天",
        )
        end_d   = date.today()
        start_d = end_d - timedelta(days=days)
    else:
        ca, cb = st.columns(2)
        with ca:
            start_d = st.date_input("开始日期", value=date.today() - timedelta(days=7))
        with cb:
            end_d   = st.date_input("结束日期", value=date.today())

    submitted = st.form_submit_button("🔍 查询", type="primary", use_container_width=True)


# ============================================================
# 渲染：基于原始两表重建时间线（新查询 / 历史回看共用）
# ============================================================

def _render_results(meta: dict, df_login: pd.DataFrame, df_rebind: pd.DataFrame):
    """df_rebind 为自创角以来的全量 rebind。"""
    ts = meta.get("ts", "")[:19].replace("T", " ")
    st.markdown(
        f"### 玩家 `{meta['roleid']}`（zone `{meta['zoneid']}`） · "
        f"`{meta['start']}` → `{meta['end']}`"
    )

    if (df_login is None or df_login.empty) and (df_rebind is None or df_rebind.empty):
        st.warning("⚠️ 该玩家在所选时间段内没有任何登录 / 切换账号记录。"
                   "建议加长时间窗口或检查 roleid。")
        return

    # 异常登录检测 + 设备指纹校验标注
    create_dev = meta.get("create_device_id")
    annotated, info = annotate_logins(df_login, df_rebind, create_dev, validate_login)
    rb_window = _rebind_in_window(df_rebind, meta["start"], meta["end"])

    st.caption(
        f"查询时间：{ts} · 登录 {len(df_login)} 条 · 全量切号 {len(df_rebind)} 条"
        f"（窗口内 {len(rb_window)} 条） · 创角设备 "
        f"`{(info.get('create_device_id') or '未知')[:28]}…`"
        + ("（创角日登录已过期，用最早登录设备兜底）" if info.get("create_device_fallback") else "")
    )

    merged = build_timeline(annotated, rb_window)
    visible, blocks = compute_folding(merged)

    # 顶部风险结论
    n_abn = info.get("n_abnormal", 0)
    n_fp  = info.get("n_fp_fail", 0)
    if n_abn == 0 and n_fp == 0 and len(rb_window) == 0:
        st.success("✅ 全部登录通过设备指纹校验，无异常登录，窗口内无切换账号。")
    else:
        bits = []
        if n_abn:
            bits.append(f"🚩 **{n_abn} 条异常登录**（设备无创角/切号来源，已标红）")
        if n_fp:
            bits.append(f"🚨 **{n_fp} 条设备指纹校验失败**（已标红）")
        if len(rb_window):
            bits.append(f"🔄 **{len(rb_window)} 条切换账号**（时间线蓝色行）")
        st.error(" · ".join(bits))

    q_zone = int(meta["zoneid"])
    tab_tl, tab_rb = st.tabs(["🧭 登录时间线", "🔄 切换账号（全量）"])

    with tab_tl:
        display_df = render_timeline(merged, visible, blocks,
                                     key_prefix=f"tl_{meta['id']}",
                                     queried_zoneid=q_zone)
        if not display_df.empty:
            st.download_button(
                "📥 导出完整时间线 CSV",
                data      = display_df.to_csv(index=False).encode("utf-8-sig"),
                file_name = f"timeline_{meta['roleid']}_{meta['start']}_{meta['end']}.csv",
                mime      = "text/csv",
                key       = f"dl_tl_{meta['id']}",
            )

    with tab_rb:
        st.caption("该账号自创角以来的全部切换账号(rebind)记录（用于异常登录检测的来源依据）。")
        rb_disp = build_rebind_display(df_rebind)
        if rb_disp.empty:
            st.info("该账号没有任何切换账号(rebind)记录。")
        else:
            st.dataframe(rb_disp, use_container_width=True, hide_index=True, height=480)
            st.download_button(
                "📥 导出全部切换账号 CSV",
                data      = rb_disp.to_csv(index=False).encode("utf-8-sig"),
                file_name = f"rebind_{meta['roleid']}_{meta['start']}_{meta['end']}.csv",
                mime      = "text/csv",
                key       = f"dl_rb_{meta['id']}",
            )


# ============================================================
# 主流程：表单提交 → 跑查询 → 落盘 → 重渲染
# ============================================================

if submitted:
    roleid, zoneid = _validate(roleid_str, zoneid_str, start_d, end_d)
    start_ymd = start_d.strftime("%Y-%m-%d")
    end_ymd   = end_d.strftime("%Y-%m-%d")

    df_login   = pd.DataFrame()
    df_rebind  = pd.DataFrame()
    create_dev = None
    create_ymd = None

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
                status.update(label=f"⏳ 前方还有 {active_user_count()} 人在查询，已等 {waited}s...")

        try:
            # Step1: 窗口内登录（含 geoip）
            status.update(label="① 拉取窗口内登录日志 + geoip...")
            df_login = query_login(roleid, zoneid, start_ymd, end_ymd)
            create_ymd = _derive_create_ymd(df_login)
            st.write(f"📥 登录 {len(df_login)} 条"
                     + (f" · 创角日 {create_ymd}" if create_ymd else ""))

            # Step2: 创角设备 + 自创角以来的全量切号（异常登录检测依据；老账号可能较慢）
            status.update(label="② 拉取创角设备 + 自创角以来的全部切换账号（老账号可能稍慢）...")
            rb_start = create_ymd or start_ymd
            create_dev, df_rebind = query_aux(roleid, create_ymd, rb_start, end_ymd)
            st.write(f"🔄 切换账号 {len(df_rebind)} 条"
                     + (f" · 创角设备 `{(create_dev or '未取到')[:24]}…`"))
            status.update(label="数据库查询完成 ✓", state="complete")
        except Exception as e:
            status.update(label="数据库查询失败 ✗", state="error")
            st.error(f"数据库查询失败：{e}")
            st.stop()
        finally:
            release_slot()

    if df_login.empty and df_rebind.empty:
        st.warning("⚠️ 该玩家在所选时间段内没有任何登录 / 切换账号记录。"
                   "建议加长时间窗口或检查 roleid。")
        st.stop()

    qid = history.save(roleid, zoneid, start_ymd, end_ymd, df_login, df_rebind,
                       create_device_id=create_dev, create_ymd=create_ymd)
    st.session_state.history = history.list_all()
    st.session_state.view_id = qid
    st.rerun()


# ============================================================
# 渲染：基于 view_id 从磁盘读
# ============================================================

if st.session_state.view_id:
    try:
        meta, df_login, df_rebind = history.load(st.session_state.view_id)
    except Exception as e:
        st.error(f"加载历史记录失败：{e}")
        st.session_state.view_id = None
    else:
        _render_results(meta, df_login, df_rebind)
else:
    st.info("👆 输入 roleid + zoneid 查询，或在左侧边栏点选历史记录回看")
