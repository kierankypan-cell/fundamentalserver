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
from db import query_country, query_in_battle, query_out_battle
from prompts import COUNTRY_LANGUAGE
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

with st.sidebar:
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

    submitted = st.form_submit_button("🔍 查询", type="primary", use_container_width=True)


# ============================================================
# 工具函数
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


def _highlight_risk(row):
    level = int(row.get("风险等级", 0))
    if level >= 3: return ["background-color: #ffcccc"] * len(row)
    if level == 2: return ["background-color: #ffe0b3"] * len(row)
    if level == 1: return ["background-color: #fff5cc"] * len(row)
    return [""] * len(row)


def _enrich(df: pd.DataFrame, results: list[dict]) -> pd.DataFrame:
    df = df.copy()
    df["translation"] = [r["translation"] for r in results]
    df["risk_level"]  = [r["risk_level"]  for r in results]
    df["risk_type"]   = [r["risk_type"]   for r in results]
    df["risk_reason"] = [r["risk_reason"] for r in results]
    return df


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
        "chat_language": "频道",
        "chat_type":     "聊天类型",
    })
    cols = ["时间", "原文", "翻译", "风险等级", "风险类型", "是否被屏蔽",
            "风险原因", "频道", "聊天类型"]
    cols = [c for c in cols if c in out.columns]
    out  = out[cols]
    return out.sort_values(["风险等级", "时间"], ascending=[False, True]).reset_index(drop=True)


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


def _fmt_pct(num: int, den: int) -> str:
    """整数比例格式化为百分比；分母为 0 时返回 '-'。"""
    if den == 0:
        return "-"
    return f"{num / den * 100:.1f}%"


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


def _render_tab(df: pd.DataFrame, scene_name: str, file_prefix: str):
    if df.empty:
        st.info(f"该时间段内没有【{scene_name}】聊天记录")
        return

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
    display_df = _to_display(df)
    st.dataframe(
        display_df.style.apply(_highlight_risk, axis=1),
        use_container_width = True,
        height              = 480,
    )

    csv_bytes = display_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label     = f"📥 导出【{scene_name}】CSV",
        data      = csv_bytes,
        file_name = f"{file_prefix}.csv",
        mime      = "text/csv",
        key       = f"dl_{file_prefix}",
    )


def _build_excel(df_in: pd.DataFrame, df_out: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        if not df_in.empty:
            _to_display(df_in).to_excel(writer, sheet_name="战斗内", index=False)
        else:
            pd.DataFrame([{"提示": "该时间段内无战斗内聊天"}]).to_excel(
                writer, sheet_name="战斗内", index=False)
        if not df_out.empty:
            _to_display(df_out).to_excel(writer, sheet_name="战斗外", index=False)
        else:
            pd.DataFrame([{"提示": "该时间段内无战斗外聊天"}]).to_excel(
                writer, sheet_name="战斗外", index=False)
    buf.seek(0)
    return buf.getvalue()


def _render_results(meta: dict, df_in: pd.DataFrame, df_out: pd.DataFrame):
    """根据 meta + 两张 DF 渲染整页结果（既用于新查询也用于历史回看）。"""
    country   = meta.get("country") or "?"
    lang_hint = COUNTRY_LANGUAGE.get((country or "").upper(), "未知")
    ts        = meta.get("ts", "")[:19].replace("T", " ")

    st.markdown(
        f"### 玩家 `{meta['roleid']}` (zone `{meta['zoneid']}`) · "
        f"国家 `{country}`（主要语言：{lang_hint}） · "
        f"`{meta['start']}` → `{meta['end']}`"
    )
    st.caption(f"查询时间：{ts}")

    if df_in.empty and df_out.empty:
        st.warning("该时间段内没有任何聊天记录。")
        return

    st.markdown("---")
    st.subheader("📋 总览结论")
    st.markdown(
        f"- {_summary(df_in,  '**战斗内**')}\n"
        f"- {_summary(df_out, '**战斗外**')}"
    )

    excel_bytes = _build_excel(df_in, df_out)
    st.download_button(
        label     = "📦 导出完整报告 (Excel · 双 Sheet)",
        data      = excel_bytes,
        file_name = f"chat_audit_{meta['roleid']}_{meta['zoneid']}_{meta['start']}_{meta['end']}.xlsx",
        mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type      = "primary",
        key       = f"xlsx_{meta['id']}",
    )

    st.markdown("---")
    tab_in, tab_out = st.tabs(["⚔️ 战斗内", "🌐 战斗外"])
    file_stem = f"chat_audit_{meta['roleid']}_{meta['zoneid']}_{meta['start']}_{meta['end']}"
    with tab_in:
        _render_tab(df_in,  "战斗内",  f"{file_stem}_in")
    with tab_out:
        _render_tab(df_out, "战斗外",  f"{file_stem}_out")


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

    with st.status("正在查询数据库 + 检测国家...", expanded=True) as status:
        try:
            st.write("📍 检测玩家国家（geoip 最近一次登录的 client_ip）...")
            country = query_country(roleid, zoneid, start_ymd, end_ymd)
            if country:
                lang = COUNTRY_LANGUAGE.get(country, "未在常见列表内")
                st.write(f"    → 国家 `{country}`（主要语言：{lang}）")
            else:
                st.write("    → ⚠️ 未找到登录记录或 IP 无法定位，翻译时不附加语言提示")

            st.write("📥 战斗外（含 zoneid 过滤）...")
            df_out = query_out_battle(roleid, zoneid, start_ymd, end_ymd)
            st.write(f"    → 返回 {len(df_out)} 条")

            st.write("📥 战斗内（无 zoneid，按 sender 过滤）...")
            df_in = query_in_battle(roleid, start_ymd, end_ymd)
            st.write(f"    → 返回 {len(df_in)} 条")

            status.update(label="数据库查询完成 ✓", state="complete")
        except Exception as e:
            status.update(label="数据库查询失败 ✗", state="error")
            st.error(f"数据库查询失败：{e}")
            st.stop()

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
    qid = history.save(roleid, zoneid, country, start_ymd, end_ymd, df_in, df_out)
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
