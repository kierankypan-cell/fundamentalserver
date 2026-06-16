"""
app.py —— 玩家聊天风险查询工具（Streamlit 主入口）

启动方式：
    streamlit run chat_query_tool/app.py

功能：
- 输入 roleid + zoneid + 时长，先 geoip 检测玩家所在国家
- 拉取战斗内 / 战斗外聊天，按对应语种 Sonnet 4.6 翻译评估
- 总览 + 双 Tab 详细报告 + CSV / Excel 导出
- 左侧边栏保留最多 20 条历史，点选可重看与重新下载
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from analyzer import analyze_in_battle, analyze_out_battle
from db import (
    MAX_CONCURRENT_USERS,
    active_user_count,
    query_user_chats,
    release_slot,
    try_acquire_slot,
)
from prompts import COUNTRY_LANGUAGE
from ui_helpers import (
    SCOPE_OPTIONS,
    SCOPE_TO_CODE,
    _validate,
    _highlight_risk,
    _to_display,
    _summary,
    _overview_table,
    _shield_table,
    _breakdown,
    _render_chat_table_view,
)
import history


# ============================================================
# 页面配置 + session_state
# ============================================================

st.set_page_config(
    page_title = "玩家聊天风险查询",
    page_icon  = "🎮",
    layout     = "wide",
)

if "history" not in st.session_state:
    st.session_state.history = history.list_all()
if "view_id" not in st.session_state:
    st.session_state.view_id = None


# ============================================================
# 侧边栏：历史记录
# ============================================================

# 隐藏 streamlit 自动从 pages/ 目录推断出的侧边栏导航（默认会显示文件名 "app"），
# 改用 st.page_link 自己渲染两个链接，可控 label + icon
st.markdown(
    "<style>[data-testid='stSidebarNav']{display:none;}</style>",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.page_link("app.py",                       label="发言审查", icon="💬")
    st.page_link("pages/2_💔_收信审查.py",       label="收信审查", icon="💔")
    st.page_link("pages/3_💬_私聊审查.py",       label="私聊审查", icon="🗨️")
    st.divider()

    st.header("📜 查询历史")
    st.caption(f"最多保留 {history.MAX_HISTORY} 条 · 落盘到 chat_query_tool/history/")

    if not st.session_state.history:
        st.info("（暂无历史记录）")
    else:
        for h in st.session_state.history:
            ts      = h.get("ts", "")[:19].replace("T", " ")
            country = h.get("country") or "?"
            counts  = f"内 {h.get('in_count', 0)} / 外 {h.get('out_count', 0)}"
            label   = f"{ts}\n{h['roleid']} · zone {h['zoneid']} · {country} · {counts}"
            active  = h["id"] == st.session_state.view_id
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

st.title("🎮 玩家聊天风险查询工具")
st.caption("输入玩家 roleid + zoneid + 时长，自动检测国家并按对应语种翻译评估")

# 时长模式放到 form 外面：form 内的 widget 不会即时 rerun，
# 放进去会导致切换"自定义日期"后 date_input 不出现。
mode = st.radio("时长模式", ["预设时长", "自定义日期"], horizontal=True, key="mode")

with st.form("query_form"):
    col1, col2 = st.columns(2)
    with col1:
        roleid_str = st.text_input("玩家 roleid", placeholder="例如 8677905")
    with col2:
        zoneid_str = st.text_input("zoneid",      placeholder="例如 4001")

    if mode == "预设时长":
        days = st.selectbox(
            "时长",
            options       = [1, 3, 7, 14, 30],
            index         = 2,
            format_func   = lambda d: f"近 {d} 天",
        )
        end_d   = date.today()
        start_d = end_d - timedelta(days=days)
    else:
        ca, cb = st.columns(2)
        with ca:
            start_d = st.date_input("开始日期", value=date.today() - timedelta(days=7))
        with cb:
            end_d   = st.date_input("结束日期", value=date.today())

    scope_label = st.radio("查询范围", SCOPE_OPTIONS, horizontal=True, key="scope")
    scope = SCOPE_TO_CODE[scope_label]

    submitted = st.form_submit_button("🔍 查询", type="primary", use_container_width=True)


# ============================================================
# 工具函数（页面专属；通用辅助见 ui_helpers.py）
# ============================================================

def _enrich(df: pd.DataFrame, results: list[dict]) -> pd.DataFrame:
    df = df.copy()
    df["translation"] = [r["translation"] for r in results]
    df["risk_level"]  = [r["risk_level"]  for r in results]
    df["risk_type"]   = [r["risk_type"]   for r in results]
    df["risk_reason"] = [r["risk_reason"] for r in results]
    return df


def _stats(df: pd.DataFrame) -> dict:
    """以 AI 的 risk_level≥1 为真值，计算违规率与屏蔽混淆矩阵。"""
    risky    = df["risk_level"] >= 1
    severe   = df["risk_level"] >= 2
    shielded = df["is_shield"].astype(int) == 1

    tp = int((risky  &  shielded).sum())
    fp = int((~risky &  shielded).sum())
    fn = int((risky  & ~shielded).sum())
    tn = int((~risky & ~shielded).sum())

    return dict(
        total      = len(df),
        n_risky    = int(risky.sum()),
        n_severe   = int(severe.sum()),
        n_shielded = int(shielded.sum()),
        visible_risky = fn,
        tp=tp, fp=fp, fn=fn, tn=tn,
    )


def _render_tab(df: pd.DataFrame, scene_name: str, file_prefix: str,
                queried_zoneid: int | None = None):
    if df.empty:
        st.info(f"该时间段内没有【{scene_name}】聊天记录")
        return

    # ── 非目标区服提醒（仅战斗外含 zoneid 列）────────────────
    # 一个 roleid 可在多个区有角色；按 roleid 拉取可能混入该账号在别区的发言。
    if queried_zoneid is not None and "zoneid" in df.columns:
        off = df[df["zoneid"].astype("int64") != int(queried_zoneid)]
        if not off.empty:
            n_target = len(df) - len(off)
            other_zones = sorted(set(off["zoneid"].astype("int64").tolist()))
            st.warning(
                f"⚠️ 本次按 roleid 拉取到 {len(df)} 条战斗外发言，其中 **{len(off)} 条来自非目标区服**"
                f"（你查询的区服是 `{queried_zoneid}`，匹配 {n_target} 条；"
                f"另有其他区：{other_zones}）。同一 roleid 可能在多个区各有角色，"
                f"这些非目标区的发言**可能不是你要查的那个角色**——明细表「是否目标区服」列已标注。"
            )

    s = _stats(df)

    # ── 一句话结论 ────────────────────────────────────────
    if s["n_risky"] == 0:
        st.success(f"✅ 该玩家在【{scene_name}】场景下未发现违规内容（共 {s['total']} 条聊天）")
    elif s["visible_risky"] == 0:
        st.warning(f"⚠️ 检测到 {s['n_risky']} 条违规内容，但**全部被屏蔽**，其他玩家看不到")
    else:
        st.error(
            f"🚨 检测到 {s['n_risky']} 条违规内容（严重 {s['n_severe']} 条），"
            f"其中 **{s['visible_risky']} 条未被屏蔽** — 被其他玩家看到"
        )

    # ── 总览 + 屏蔽系统性能 ───────────────────────────────
    cl, cr = st.columns(2)
    with cl:
        st.markdown("**📊 总览**")
        st.dataframe(_overview_table(s),
                     use_container_width=True, hide_index=True)
    with cr:
        st.markdown("**🛡️ 屏蔽系统性能**（以 AI 判断为真值）")
        st.dataframe(_shield_table(s),
                     use_container_width=True, hide_index=True)

    # ── 明细分布（按等级 / 类型 切换）─────────────────────
    st.markdown("**🔬 风险明细分布**")
    by_label = st.radio(
        "分组维度",
        options          = ["按风险等级", "按风险类型"],
        horizontal       = True,
        label_visibility = "collapsed",
        key              = f"breakdown_{file_prefix}",
    )
    by_col = "risk_level" if by_label == "按风险等级" else "risk_type"
    st.dataframe(_breakdown(df, by_col),
                 use_container_width=True, hide_index=True)

    # ── 聊天明细 ──────────────────────────────────────────
    st.markdown("**📝 聊天明细**")
    display_df = _to_display(df, queried_zoneid)
    _render_chat_table_view(
        display_df,
        highlight_fn = _highlight_risk,
        key_prefix   = f"chat_{file_prefix}",
    )

    csv_bytes = display_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label     = f"📥 导出【{scene_name}】CSV",
        data      = csv_bytes,
        file_name = f"{file_prefix}.csv",
        mime      = "text/csv",
        key       = f"dl_{file_prefix}",
    )


def _build_excel(df_in: pd.DataFrame, df_out: pd.DataFrame,
                 scope: str = "all", queried_zoneid: int | None = None) -> bytes:
    """scope 决定空 sheet 的占位文案：被本次查询排除时显示「未查询」而非「无记录」。
    queried_zoneid 传给战斗外，使导出也带「区服 / 是否目标区服」标记。"""
    in_empty_hint  = ("（本次未查询该场景）" if scope == "out"
                      else "该时间段内无战斗内聊天")
    out_empty_hint = ("（本次未查询该场景）" if scope == "in"
                      else "该时间段内无战斗外聊天")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        if not df_in.empty:
            _to_display(df_in).to_excel(writer, sheet_name="战斗内", index=False)
        else:
            pd.DataFrame([{"提示": in_empty_hint}]).to_excel(
                writer, sheet_name="战斗内", index=False)
        if not df_out.empty:
            _to_display(df_out, queried_zoneid).to_excel(
                writer, sheet_name="战斗外", index=False)
        else:
            pd.DataFrame([{"提示": out_empty_hint}]).to_excel(
                writer, sheet_name="战斗外", index=False)
    buf.seek(0)
    return buf.getvalue()


def _render_results(meta: dict, df_in: pd.DataFrame, df_out: pd.DataFrame):
    """根据 meta + 两张 DF 渲染整页结果（既用于新查询也用于历史回看）。"""
    country   = meta.get("country") or "?"
    lang_hint = COUNTRY_LANGUAGE.get((country or "").upper(), "未知")
    ts        = meta.get("ts", "")[:19].replace("T", " ")
    scope     = meta.get("scope", "all")    # 旧历史无此字段，默认全部
    show_in   = scope in ("all", "in")
    show_out  = scope in ("all", "out")

    scope_note = {"in": "（仅战斗内）", "out": "（仅战斗外）"}.get(scope, "")
    st.markdown(
        f"### 玩家 `{meta['roleid']}` (zone `{meta['zoneid']}`) · "
        f"国家 `{country}`（主要语言：{lang_hint}） · "
        f"`{meta['start']}` → `{meta['end']}` {scope_note}"
    )
    st.caption(f"查询时间：{ts}")

    if df_in.empty and df_out.empty:
        st.warning("该时间段内没有任何聊天记录。")
        return

    st.markdown("---")
    st.subheader("📋 总览结论")
    bullets = []
    if show_in:
        bullets.append(f"- {_summary(df_in,  '**战斗内**')}")
    if show_out:
        bullets.append(f"- {_summary(df_out, '**战斗外**')}")
    st.markdown("\n".join(bullets))

    q_zone = int(meta["zoneid"])
    excel_bytes = _build_excel(df_in, df_out, scope, q_zone)
    st.download_button(
        label     = "📦 导出完整报告 (Excel · 双 Sheet)",
        data      = excel_bytes,
        file_name = f"chat_audit_{meta['roleid']}_{meta['zoneid']}_{meta['start']}_{meta['end']}.xlsx",
        mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type      = "primary",
        key       = f"xlsx_{meta['id']}",
    )

    st.markdown("---")
    file_stem = f"chat_audit_{meta['roleid']}_{meta['zoneid']}_{meta['start']}_{meta['end']}"
    if show_in and show_out:
        tab_in, tab_out = st.tabs(["⚔️ 战斗内", "🌐 战斗外"])
        with tab_in:
            _render_tab(df_in,  "战斗内",  f"{file_stem}_in")
        with tab_out:
            _render_tab(df_out, "战斗外",  f"{file_stem}_out", queried_zoneid=q_zone)
    elif show_in:
        _render_tab(df_in,  "战斗内",  f"{file_stem}_in")
    else:
        _render_tab(df_out, "战斗外",  f"{file_stem}_out", queried_zoneid=q_zone)


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
        # ── Step 0: 排队获取并发配额（同时最多 MAX_CONCURRENT_USERS 个用户在查）
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

        # ── Step 1: 拿到配额后并发跑三条 SQL（finally 必释放配额）
        try:
            status.update(label="并发执行：geoip 检测 + 战斗内 + 战斗外...")
            country, df_out, df_in = query_user_chats(
                roleid, zoneid, start_ymd, end_ymd, scope
            )

            if country:
                lang = COUNTRY_LANGUAGE.get(country, "未在常见列表内")
                st.write(f"📍 国家 `{country}`(主要语言：{lang})")
            else:
                st.write("📍 ⚠️ 未找到登录记录或 IP 无法定位，翻译时不附加语言提示")

            st.write(f"📥 战斗外 {len(df_out)} 条 / 战斗内 {len(df_in)} 条")
            status.update(label="数据库查询完成 ✓", state="complete")
        except Exception as e:
            status.update(label="数据库查询失败 ✗", state="error")
            st.error(f"数据库查询失败：{e}")
            st.stop()           # st.stop 会抛特殊异常，仍会触发下面的 finally
        finally:
            release_slot()      # 不论正常 / 异常 / st.stop，配额都准确释放一次

    if df_in.empty and df_out.empty:
        st.warning("⚠️ 该玩家在所选时间段内没有任何聊天记录。建议加长时间窗口或检查 roleid / zoneid。")
        st.stop()

    if not df_out.empty:
        st.write("**战斗外** 内容分析中...")
        bar = st.progress(0.0, text="准备中")
        try:
            results_out = analyze_out_battle(
                df_out["content"].fillna("").astype(str).tolist(),
                progress_callback = lambda cur, total: bar.progress(
                    cur / total, text=f"批次 {cur}/{total}"),
                country = country,
            )
            df_out = _enrich(df_out, results_out)
            bar.empty()
        except Exception as e:
            bar.empty()
            st.error(f"战斗外分析失败：{e}")
            st.stop()

    if not df_in.empty:
        st.write("**战斗内** 内容分析中...")
        bar = st.progress(0.0, text="准备中")
        try:
            results_in = analyze_in_battle(
                df_in["content"].fillna("").astype(str).tolist(),
                progress_callback = lambda cur, total: bar.progress(
                    cur / total, text=f"批次 {cur}/{total}"),
                country = country,
            )
            df_in = _enrich(df_in, results_in)
            bar.empty()
        except Exception as e:
            bar.empty()
            st.error(f"战斗内分析失败：{e}")
            st.stop()

    # 落盘 + 切到新记录 → rerun 后从磁盘读取并渲染（统一渲染路径）
    qid = history.save(roleid, zoneid, country, start_ymd, end_ymd,
                       df_in, df_out, scope=scope)
    st.session_state.history = history.list_all()
    st.session_state.view_id = qid
    st.rerun()


# ============================================================
# 渲染：基于 view_id 从磁盘读
# ============================================================

if st.session_state.view_id:
    try:
        meta, df_in, df_out = history.load(st.session_state.view_id)
    except Exception as e:
        st.error(f"加载历史记录失败：{e}")
        st.session_state.view_id = None
    else:
        _render_results(meta, df_in, df_out)
else:
    st.info("👆 输入 roleid + zoneid 查询，或在左侧边栏点选历史记录回看")
