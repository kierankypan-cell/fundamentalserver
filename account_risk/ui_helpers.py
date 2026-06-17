"""
ui_helpers.py —— UI 渲染辅助（account_risk）

仅含纯函数 / 渲染函数，**不执行任何顶层 streamlit 渲染**，供 app.py import。

包含：
    _validate                 —— 表单输入校验
    fmt_time                  —— 时间戳 → "yyyy/mm/dd hh:mm:ss"
    COL_SPECS / DEFAULT_COLS  —— 登录时间线默认展示列（状态/校验结果前置，便于查看）
    build_display             —— merged 行 → 展示 DataFrame（login/rebind 字段名归一）
    build_rebind_display      —— 纯切换账号表展示
    render_timeline           —— 合并时间线：行内折叠按钮(展开/收起) + 行高亮 + 列选择
"""

from __future__ import annotations

import html as _html
from datetime import date

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


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
# 时间格式化
# ============================================================

def fmt_time(v) -> str:
    """任意时间值 → 'yyyy/mm/dd hh:mm:ss'；无法解析返回原值 / 空串。"""
    ts = pd.to_datetime(v, errors="coerce")
    if pd.isna(ts):
        return "" if v is None else str(v)
    return ts.strftime("%Y/%m/%d %H:%M:%S")


# ============================================================
# 展示列定义（req5：状态图标 + 校验结果 前置，避免左右拖动）
# ============================================================
# COL_SPECS：展示列名 -> 取值来源
#   ("col", "x")            直接取 merged["x"]
#   ("coalesce", "a", "b")  login 取 a、rebind 取 b（字段名差异归一）
#   ("status",) / ("kind",) / ("note",)  特殊计算列

COL_SPECS: dict[str, tuple] = {
    "状态":         ("status",),
    "校验结果/备注": ("note",),
    "类型":         ("kind",),
    "时间":         ("col", "time"),
    "roleid":       ("roidpair",),
    "区服":         ("col", "zoneid"),
    "level":        ("col", "level"),
    "device":       ("coalesce", "device", "device_name"),
    "os_type":      ("col", "os_type"),
    "reconn":       ("col", "reconn"),
    "channel":      ("col", "channel"),
    "createrole_country": ("col", "createrole_country"),
    "client_ip":    ("col", "client_ip"),
    "城市(geoip3)":  ("col", "geo_city"),
    "网络供应商(geoip5)": ("col", "geo_isp"),
    "client_ver":   ("col", "client_ver"),
    "deviceid":     ("dev_or_acct",),
    "client_real_ver": ("col", "client_real_ver"),
    "device_uniqueid": ("coalesce", "device_uniqueid", "uniqueid"),
    "android_id":   ("col", "android_id"),
    "ad_id":        ("col", "ad_id"),
    "idfa":         ("col", "idfa"),
}

DEFAULT_COLS = list(COL_SPECS.keys())

# 不作为「更多字段」候选的原始列（已映射 / 内部辅助）
_HIDDEN_RAW = {
    "time", "roleid", "zoneid", "level", "device", "os_type", "reconn", "channel",
    "createrole_country", "client_ip", "geo_city", "geo_isp", "client_ver",
    "deviceid", "client_real_ver", "device_uniqueid", "android_id", "ad_id",
    "idfa", "device_name", "device_id", "uniqueid", "new_accountid",
}


def _fmt_id(v) -> str:
    """账号 ID 去掉浮点尾巴（999.0 → 999）。"""
    s = "" if v is None else str(v).strip()
    if s in ("", "none", "None", "nan"):
        return ""
    try:
        return str(int(float(s)))
    except (ValueError, TypeError):
        return s


def _cell(mr: pd.Series, spec: tuple):
    kind = spec[0]
    if kind == "col":
        return mr.get(spec[1], "")
    if kind == "coalesce":
        is_login = mr.get("_type") == "login"
        return mr.get(spec[1] if is_login else spec[2], "")
    if kind == "roidpair":
        # login 显示 roleid；rebind 显示 old_accountid→new_accountid
        if mr.get("_type") == "rebind":
            return f"{_fmt_id(mr.get('old_accountid'))}→{_fmt_id(mr.get('new_accountid'))}"
        return _fmt_id(mr.get("roleid"))
    if kind == "dev_or_acct":
        # login 显示 deviceid；rebind 显示 account_name（与 login.deviceid 同源，便于对齐比对）
        if mr.get("_type") == "rebind":
            return mr.get("account_name", "")
        return mr.get("deviceid", "")
    if kind == "kind":
        return "🔄 切换账号" if mr.get("_type") == "rebind" else "登录"
    if kind == "status":
        if mr.get("_type") == "rebind":
            return "🔄"
        return "🚨" if bool(mr.get("_valid_fail")) else "✅"
    if kind == "note":
        if mr.get("_type") == "rebind":
            return _rebind_summary(mr)
        return mr.get("_reasons", "") or "✅ 通过"
    return ""


def _rebind_summary(r: pd.Series) -> str:
    """rebind 行在时间线里的一句话备注（old→new 已在 roleid 列展示，这里只补充其余信息）。"""
    bindnum  = _fmt_id(r.get("old_accountid_bindnum"))
    verified = str(r.get("is_email_verified", "")).strip() in ("1", "1.0")
    octype   = str(r.get("old_account_type", "")).strip()
    parts = ["切换账号"]
    if octype and octype.lower() != "none":
        parts.append(f"原账号类型={octype}")
    if bindnum:
        parts.append(f"原账号绑定数={bindnum}")
    parts.append(f"强制验证={'是' if verified else '否'}")
    return "；".join(parts)


# ============================================================
# 展示 DataFrame 构建
# ============================================================

def build_display(merged: pd.DataFrame,
                  extra_cols: list[str] | None = None) -> pd.DataFrame:
    """把若干 merged 行转成展示 DataFrame（默认列 + 可选「更多字段」）。"""
    rows = []
    for _, mr in merged.iterrows():
        d = {}
        for disp_name, spec in COL_SPECS.items():
            val = _cell(mr, spec)
            d[disp_name] = fmt_time(val) if disp_name == "时间" else val
        for c in (extra_cols or []):
            d[c] = mr.get(c, "")
        rows.append(d)

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    ordered = DEFAULT_COLS + [c for c in (extra_cols or []) if c in out.columns]
    ordered = [c for c in ordered if c in out.columns]
    return out[ordered].reset_index(drop=True)


def extra_col_options(merged: pd.DataFrame) -> list[str]:
    if merged is None or merged.empty:
        return []
    return sorted(
        c for c in merged.columns
        if not c.startswith("_") and c not in _HIDDEN_RAW
    )


# ============================================================
# 切换账号(rebind) 纯表展示
# ============================================================

_REBIND_ORDER = [
    "time", "new_accountid", "account_name", "old_accountid",
    "old_accountid_bindnum", "old_account_type", "is_email_verified",
    "os_type", "channel", "login_country", "device_id", "device_name",
    "uniqueid", "android_id", "ad_id", "idfa", "drm_id",
    "new_mtid", "old_mtid", "loghms",
]


def build_rebind_display(df_rebind: pd.DataFrame) -> pd.DataFrame:
    """纯切换账号表：时间格式化 + 关键列前置。"""
    if df_rebind is None or df_rebind.empty:
        return pd.DataFrame()
    out = df_rebind.copy()
    if "time" in out.columns:
        out["time"] = out["time"].map(fmt_time)
    cols = [c for c in _REBIND_ORDER if c in out.columns]
    cols += [c for c in out.columns if c not in cols and not c.startswith("_")]
    return out[cols].reset_index(drop=True)


# ============================================================
# 行类型 + 列宽
# ============================================================

def _row_kind(mr: pd.Series) -> str:
    if mr.get("_type") == "rebind":
        return "rebind"
    return "fail" if bool(mr.get("_valid_fail")) else "login"


_COL_W = {
    "状态": 46, "校验结果/备注": 300, "类型": 92, "时间": 150, "roleid": 120,
    "区服": 70, "level": 58, "device": 190, "os_type": 72, "reconn": 64,
    "channel": 120, "createrole_country": 120, "client_ip": 130,
    "城市(geoip3)": 210, "网络供应商(geoip5)": 190, "client_ver": 96,
    "deviceid": 330, "client_real_ver": 120, "device_uniqueid": 290,
    "android_id": 150, "ad_id": 200, "idfa": 210,
}
_COL_W_DEFAULT = 160


# ============================================================
# 单表 HTML 渲染（连续大表 + 原生 <details> 折叠 + 行内 +/- + 悬浮提示）
# ============================================================

_STYLE = """
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font: 12px/1.45 -apple-system, "Segoe UI", Arial; color: #222; }
  .ar-ctrl { padding: 4px 0 8px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .ar-ctrl button { font-size: 12px; padding: 3px 10px; cursor: pointer;
                    border: 1px solid #c4ccd6; background: #f3f6fa; border-radius: 4px; }
  .ar-ctrl button:hover { background: #e6edf6; }
  .ar-ctrl input { font-size: 12px; padding: 3px 8px; border: 1px solid #c4ccd6;
                   border-radius: 4px; width: 240px; }
  .ar-note { color: #8893a5; }
  .ar-wrap { overflow: auto; border: 1px solid #d7dde5; border-radius: 4px; }
  .ar-tbl { width: max-content; min-width: 100%; }
  .ar-row { display: flex; border-bottom: 1px solid #eef0f3; }
  .ar-row.head { position: sticky; top: 0; z-index: 3; background: #243040; color: #fff;
                 font-weight: 600; }
  .ar-cell { flex: 0 0 auto; padding: 4px 8px; white-space: nowrap; overflow: hidden;
             text-overflow: ellipsis; border-right: 1px solid #eef0f3; }
  .hcell { position: relative; display: flex; align-items: center; gap: 3px;
           cursor: pointer; user-select: none; border-right: 1px solid #3a485c; }
  .hcell:hover { background: #2f3d50; }
  .hcell .hlbl { overflow: hidden; text-overflow: ellipsis; }
  .hcell .cae { margin-left: auto; color: #9fb0c8; font-size: 10px; }
  .hcell.act .cae { color: #ffd56b; }
  .hcell .arr { color: #ffd56b; }
  .ar-resz { position: absolute; right: 0; top: 0; width: 7px; height: 100%;
             cursor: col-resize; }
  .ar-resz:hover { background: #5b9bd5; }
  .r-ok      { background: #ffffff; }
  .r-ok.even { background: #fafbfc; }
  .r-rebind  { background: #eaf1fc; }
  .st-fail   { color: #d62828; font-weight: 700; }
  .ar-pop .ar-setttl { margin-top: 8px; color: #8893a5; font-size: 11px; }
  .ar-pop .ar-set { max-height: 160px; overflow-y: auto; overflow-x: hidden;
                    border: 1px solid #e3e7ee; border-radius: 4px; margin-top: 4px; padding: 2px; }
  .ar-pop .ar-si { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                   padding: 3px 4px; cursor: pointer; }
  .ar-pop .ar-si:hover { background: #f0f4fa; }
  .ar-pop .ar-si input { vertical-align: middle; margin: 0 6px 0 0; }
  .ar-fold { display: flex; align-items: center; gap: 8px; padding: 4px 10px; cursor: pointer;
             user-select: none; color: #45526b; background: #eef1f6;
             border-bottom: 1px dashed #cdd5e0; }
  .ar-fold:hover { background: #e6ebf3; }
  .ar-fold .ico { display: inline-flex; align-items: center; justify-content: center;
        width: 16px; height: 16px; border: 1px solid #8aa0bd; border-radius: 3px; background: #fff;
        font-weight: 700; line-height: 1; color: #3a5a87; }
  .ar-grp.open { border: 1.5px solid #5b9bd5; border-radius: 6px; margin: 2px 0; overflow: hidden; }
  .ar-grp.open .ar-fold { background: #dcebff; border-bottom: 1px solid #bcd6f5; }
  .ar-back { position: fixed; inset: 0; z-index: 40; background: transparent; }
  .ar-pop { position: fixed; z-index: 50; background: #fff; border: 1px solid #aab4c4;
            border-radius: 6px; box-shadow: 0 6px 20px rgba(0,0,0,.18); padding: 10px; width: 244px; }
  .ar-pop .pt { font-weight: 600; margin-bottom: 6px; word-break: break-all; }
  .ar-pop .ps { display: flex; gap: 4px; margin-bottom: 8px; }
  .ar-pop .ps button { flex: 1; padding: 4px 0; font-size: 12px; cursor: pointer;
            border: 1px solid #c4ccd6; background: #f3f6fa; border-radius: 4px; }
  .ar-pop .ps button.on { background: #5b9bd5; color: #fff; border-color: #4a8ac4; }
  .ar-pop input[type="text"] { width: 100%; padding: 4px 6px; border: 1px solid #c4ccd6;
                  border-radius: 4px; font-size: 12px; }
  .ar-pop .pf { text-align: right; margin-top: 8px; }
  .ar-pop .pf button { font-size: 12px; padding: 3px 10px; cursor: pointer;
            border: 1px solid #c4ccd6; background: #f3f6fa; border-radius: 4px; }
</style>
"""


def _esc(v) -> str:
    return _html.escape("" if v is None else str(v))


def _build_table_html(merged: pd.DataFrame, visible: list[bool],
                      blocks: list[dict], extra: list[str], wrap_h: int) -> str:
    """
    输出一个自包含的 JS 数据网格（单张连续表）：
      - 默认分组折叠视图（连续相同设备登录折叠中间、首尾可见），行内 ＋/− 就地展开；
      - 点表头排序（升/降/恢复），全局关键词筛选；排序或筛选时自动平铺展开全部行；
      - 行底色：红=校验失败/异常、蓝=切换账号；悬浮状态/校验结果看完整原因。
    """
    import json

    disp = build_display(merged, extra)
    cols = list(disp.columns)
    kinds = [_row_kind(mr) for _, mr in merged.iterrows()]
    reasons = (disp["校验结果/备注"].tolist()
               if "校验结果/备注" in disp.columns else [""] * len(disp))

    # 每行 → 所属折叠块下标（-1 表示默认可见）
    idx_block = [-1] * len(merged)
    for bi, b in enumerate(blocks):
        for t in range(b["start"], b["end"] + 1):
            idx_block[t] = bi

    rows_data = []
    for i in range(len(disp)):
        rows_data.append({
            "v": [("" if pd.isna(disp.iloc[i][c]) else str(disp.iloc[i][c])) for c in cols],
            "k": kinds[i],
            "r": "" if (i >= len(reasons) or reasons[i] is None) else str(reasons[i]),
            "g": idx_block[i],
        })
    groups = [{"s": b["start"], "e": b["end"], "c": b["count"],
               "d": (b["device"] or "·")[:48]} for b in blocks]
    widths = [_COL_W.get(c, _COL_W_DEFAULT) for c in cols]

    payload = json.dumps({
        "cols": cols, "widths": widths, "rows": rows_data, "groups": groups,
        "wrapH": wrap_h,
    }, ensure_ascii=False).replace("</", "<\\/")

    return _STYLE + '<div id="ar-root"></div>' + "<script>\nconst AR=" + payload + ";\n" + _AR_JS + "\n</script>"


# 纯前端数据网格（运行在 components.html 的 iframe 内）：
#   分组折叠 + 关键词筛选 + 按列筛选/排序（点表头弹框选择）+ 拖拽调列宽 + 每格悬浮看全文
_AR_JS = r"""
const COLS=AR.cols, ROWS=AR.rows, GROUPS=AR.groups;
let W=AR.widths.slice();                 // 可变列宽
let sortCol=-1, sortDir=0;               // sortDir: 0 none,1 asc,-1 desc
let gFilter="";                          // 全局关键词
let colF={};                             // 按列文本筛选 {j: text}
let colSel={};                           // 按列值集筛选 {j: [values]}（弹框里勾选）
let popCol=null, popX=0, popY=0;         // 当前打开的表头弹框
let refocus=null;                        // render 后需要重新聚焦的输入（"g" 全局 / "p" 弹框）+ 光标位
let hideReb=false;                       // 是否屏蔽全部切号行
const expanded=new Set();
const NREB=ROWS.filter(r=>r.k==="rebind").length;
const root=document.getElementById("ar-root");

function esc(s){return (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function anyColF(){ return Object.keys(colF).some(k=>colF[k]); }
function anyColSel(){ return Object.keys(colSel).some(k=>colSel[k]&&colSel[k].length); }
function distinct(j){ const s=new Set(); for(let i=0;i<ROWS.length;i++) s.add(ROWS[i].v[j]); return Array.from(s); }

function cell(j,val,title,cls){
  const t=(title!=null&&title!=="")?(' title="'+esc(title)+'"'):"";
  const cc=cls?(" "+cls):"";
  return '<div class="ar-cell'+cc+'" data-j="'+j+'" style="width:'+W[j]+'px"'+t+'>'+esc(val)+'</div>';
}
function rowEl(r,even){
  let cls=r.k==="rebind"?"r-rebind":("r-ok"+(even?" even":""));   // 不再整行标红，看状态列即可
  let h='<div class="ar-row '+cls+'">';
  for(let j=0;j<COLS.length;j++){
    const c=COLS[j];
    if(c==="状态"){ h+=cell(j,r.v[j],r.r,r.k==="fail"?"st-fail":null); }   // 状态列：失败红字 + 悬浮看原因
    else { h+=cell(j,r.v[j],r.v[j],null); }                                // 其余列：悬浮看全文
  }
  return h+'</div>';
}
function headEl(){
  let h='<div class="ar-row head">';
  for(let j=0;j<COLS.length;j++){
    const hasF=colF[j]||(colSel[j]&&colSel[j].length);
    const act=(sortCol===j)||hasF;
    const arr= sortCol===j ? '<span class="arr">'+(sortDir===1?"▲":"▼")+'</span>' : '';
    h+='<div class="ar-cell hcell'+(act?' act':'')+'" data-j="'+j+'" style="width:'+W[j]+'px">'
       +'<span class="hlbl">'+esc(COLS[j])+'</span>'+arr
       +'<span class="cae">'+(hasF?'⏷●':'⏷')+'</span>'
       +'<span class="ar-resz" data-j="'+j+'"></span></div>';
  }
  return h+'</div>';
}
function cmp(a,b){
  const fa=parseFloat(a), fb=parseFloat(b);
  const na=(a!==""&&!isNaN(fa)), nb=(b!==""&&!isNaN(fb));
  if(na&&nb) return fa-fb;
  return String(a).localeCompare(String(b),"zh");
}
function matches(r){
  if(gFilter){ const q=gFilter.toLowerCase();
    let any=false; for(let j=0;j<r.v.length;j++){ if(String(r.v[j]).toLowerCase().indexOf(q)>=0){any=true;break;} }
    if(!any) return false; }
  for(const k in colF){ if(colF[k] && String(r.v[k]).toLowerCase().indexOf(colF[k].toLowerCase())<0) return false; }
  for(const k in colSel){ const s=colSel[k]; if(s&&s.length&&s.indexOf(String(r.v[k]))<0) return false; }
  return true;
}
function popHtml(){
  if(popCol==null) return "";
  const j=popCol;
  const left=Math.max(4, Math.min(popX, window.innerWidth-252));
  const dv=distinct(j);
  let setHtml="";
  if(dv.length<=40){
    const sel=colSel[j]||[];
    const items=dv.slice().sort((a,b)=>String(a).localeCompare(String(b),"zh")).map(v=>
      '<label class="ar-si" title="'+esc(v)+'"><input type="checkbox" class="ar-sc" data-v="'+esc(v)+'"'
      +(sel.indexOf(String(v))>=0?' checked':'')+'>'+(String(v)===""?'(空)':esc(v))+'</label>').join('');
    setHtml='<div class="ar-setttl">按值筛选（勾选）</div><div class="ar-set">'+items+'</div>';
  }else{
    setHtml='<div class="ar-note" style="margin-top:6px">该列取值较多（'+dv.length+' 种），请用上方文本筛选</div>';
  }
  return '<div class="ar-back"></div>'
    +'<div class="ar-pop" style="left:'+left+'px;top:'+popY+'px">'
    +'<div class="pt">'+esc(COLS[j])+'</div>'
    +'<div class="ps"><button data-d="1" class="'+(sortCol===j&&sortDir===1?"on":"")+'">↑ 升序</button>'
    +'<button data-d="-1" class="'+(sortCol===j&&sortDir===-1?"on":"")+'">↓ 降序</button>'
    +'<button data-d="0" class="'+(sortCol!==j?"on":"")+'">不排序</button></div>'
    +'<input id="ar-cf" type="text" placeholder="筛选：包含…" value="'+esc(colF[j]||"")+'">'
    +setHtml
    +'<div class="pf"><button id="ar-cfclear">清除此列</button> <button id="ar-popclose">关闭</button></div>'
    +'</div>';
}
function render(){
  // 记住表格滚动位置，避免展开/收起后弹回顶部
  const _w=root.querySelector(".ar-wrap"); const _sT=_w?_w.scrollTop:0, _sL=_w?_w.scrollLeft:0;
  const flat = (sortDir!==0) || (gFilter!=="") || anyColF() || anyColSel();
  let body="";
  if(flat){
    let list=ROWS.filter(r=>(!hideReb||r.k!=="rebind")&&matches(r));
    if(sortDir!==0){ list=list.slice().sort((x,y)=>sortDir*cmp(x.v[sortCol],y.v[sortCol])); }
    for(let k=0;k<list.length;k++){ body+=rowEl(list[k],k%2===1); }
    if(list.length===0) body='<div class="ar-row"><div class="ar-cell" style="width:400px">（无匹配记录）</div></div>';
  }else{
    let i=0,parity=0;
    while(i<ROWS.length){
      const r=ROWS[i];
      if(r.g<0){ if(hideReb&&r.k==="rebind"){ i++; continue; } body+=rowEl(r,parity%2===1); parity++; i++; }
      else{
        const g=GROUPS[r.g];
        if(expanded.has(r.g)){
          body+='<div class="ar-grp open"><div class="ar-fold" data-g="'+r.g+'"><span class="ico">−</span><b>收起这 '+g.c+' 条</b>&nbsp;·&nbsp;'+esc(g.d)+'&nbsp;<span class="ar-note">（#'+(g.s+1)+'–#'+(g.e+1)+'）</span></div>';
          for(let t=g.s;t<=g.e;t++){ body+=rowEl(ROWS[t],parity%2===1); parity++; }
          body+='</div>';
        }else{
          body+='<div class="ar-fold" data-g="'+r.g+'"><span class="ico">+</span><b>折叠 '+g.c+' 条相同设备连续登录</b>&nbsp;·&nbsp;'+esc(g.d)+'&nbsp;<span class="ar-note">（#'+(g.s+1)+'–#'+(g.e+1)+'，点击展开）</span></div>';
        }
        i=g.e+1;
      }
    }
  }
  const note = flat
    ? '<span class="ar-note">排序/筛选中：已平铺全部匹配行。</span>'
    : '<span class="ar-note">点表头弹框排序/按列筛选；拖列右边缘调宽；悬浮单元格看全文；点折叠条展开。</span>';
  root.innerHTML =
    '<div class="ar-ctrl">'
    +'<input id="ar-q" type="text" placeholder="🔍 全局关键词（所有列）" value="'+esc(gFilter)+'">'
    +'<button id="ar-open">展开全部</button><button id="ar-close">收起全部</button>'
    +(NREB?'<button id="ar-reb">'+(hideReb?'显示切号('+NREB+')':'屏蔽切号('+NREB+')')+'</button>':'')
    +((sortDir||gFilter||anyColF()||anyColSel())?'<button id="ar-reset">清除全部排序/筛选</button>':'')
    +note+'</div>'
    +'<div class="ar-wrap" style="max-height:'+AR.wrapH+'px"><div class="ar-tbl">'+headEl()+body+'</div></div>'
    +popHtml();
  wire();
  const _w2=root.querySelector(".ar-wrap"); if(_w2){ _w2.scrollTop=_sT; _w2.scrollLeft=_sL; }
  if(refocus){ const el=document.getElementById(refocus.id); if(el){ el.focus(); try{el.setSelectionRange(refocus.p,refocus.p);}catch(_){} } refocus=null; }
}
function wire(){
  const q=document.getElementById("ar-q");
  q.oninput=e=>{ gFilter=e.target.value; refocus={id:"ar-q",p:q.selectionStart}; render(); };
  document.getElementById("ar-open").onclick=()=>{ for(let i=0;i<GROUPS.length;i++)expanded.add(i); render(); };
  document.getElementById("ar-close").onclick=()=>{ expanded.clear(); render(); };
  const rb=document.getElementById("ar-reb"); if(rb) rb.onclick=()=>{ hideReb=!hideReb; render(); };
  const rs=document.getElementById("ar-reset"); if(rs) rs.onclick=()=>{ sortCol=-1;sortDir=0;gFilter="";colF={};colSel={}; render(); };

  // 表头：点开弹框；拖右边缘调列宽
  root.querySelectorAll(".hcell").forEach(c=>{
    c.onclick=e=>{ if(e.target.classList.contains("ar-resz")) return;
      const j=+c.dataset.j, rc=c.getBoundingClientRect();
      popCol=j; popX=rc.left; popY=rc.bottom+2; render(); };
  });
  root.querySelectorAll(".ar-resz").forEach(rz=>{
    rz.onclick=e=>e.stopPropagation();
    rz.onmousedown=e=>{ e.preventDefault(); e.stopPropagation();
      const j=+rz.dataset.j, sx=e.clientX, sw=W[j];
      const mv=ev=>{ W[j]=Math.max(40, sw+(ev.clientX-sx));
        root.querySelectorAll('.ar-cell[data-j="'+j+'"]').forEach(n=>{ n.style.width=W[j]+'px'; }); };
      const up=()=>{ document.removeEventListener("mousemove",mv); document.removeEventListener("mouseup",up); };
      document.addEventListener("mousemove",mv); document.addEventListener("mouseup",up);
    };
  });

  // 折叠条
  root.querySelectorAll(".ar-fold").forEach(f=>{
    f.onclick=()=>{ const g=+f.dataset.g; if(expanded.has(g))expanded.delete(g); else expanded.add(g); render(); };
  });

  // 弹框
  const bk=document.querySelector(".ar-back"); if(bk) bk.onclick=()=>{ popCol=null; render(); };
  if(popCol!=null){
    document.querySelectorAll(".ar-pop .ps button").forEach(b=>{
      b.onclick=()=>{ const d=+b.dataset.d; if(d===0){sortCol=-1;sortDir=0;} else {sortCol=popCol;sortDir=d;} render(); };
    });
    const cf=document.getElementById("ar-cf");
    cf.oninput=e=>{ const v=e.target.value; if(v) colF[popCol]=v; else delete colF[popCol];
      refocus={id:"ar-cf",p:cf.selectionStart}; render(); };
    root.querySelectorAll(".ar-sc").forEach(cb=>{ cb.onchange=()=>{ const v=cb.dataset.v;
      let s=(colSel[popCol]||[]).slice();
      if(cb.checked){ if(s.indexOf(v)<0) s.push(v); } else { s=s.filter(x=>x!==v); }
      if(s.length) colSel[popCol]=s; else delete colSel[popCol]; render(); }; });
    document.getElementById("ar-cfclear").onclick=()=>{ delete colF[popCol]; delete colSel[popCol]; render(); };
    document.getElementById("ar-popclose").onclick=()=>{ popCol=null; render(); };
  }
}
render();
"""


def render_timeline(merged: pd.DataFrame,
                    visible: list[bool],
                    blocks: list[dict],
                    key_prefix: str,
                    queried_zoneid: int | None = None) -> pd.DataFrame:
    """
    渲染登录+切换账号合并时间线为**单张连续 HTML 表**：
      - 连续相同设备登录折叠为一个原生 <details>，行内 + 展开（变 −）、就地框选展开区，再点收起；
      - 顶部「展开全部 / 收起全部」按钮（纯前端，无需重跑）；
      - 校验失败/异常=红、切换账号=蓝；悬浮状态/校验结果单元格看完整原因。
    返回全量展示 DataFrame（供 CSV 导出）。
    """
    if merged is None or merged.empty:
        st.info("该时间段内没有登录记录。")
        return pd.DataFrame()

    n = len(merged)

    # 非目标区服提醒（req2：拎到表格外说明，不再每行加列）
    if queried_zoneid is not None and "zoneid" in merged.columns:
        login_mask = merged["_type"] == "login"
        zser = pd.to_numeric(merged.loc[login_mask, "zoneid"], errors="coerce")
        off = int((zser != int(queried_zoneid)).sum())
        if off > 0:
            other = sorted({int(z) for z in zser.dropna().tolist()
                            if int(z) != int(queried_zoneid)})
            st.warning(
                f"⚠️ 按 roleid 拉取到 {int(login_mask.sum())} 条登录，其中 **{off} 条来自非目标区服 "
                f"`{queried_zoneid}`**（其他区：{other}）。同一 roleid 可能跨多个区，"
                f"这些行可能是该账号在别区的角色。"
            )

    extra = st.multiselect(
        "显示更多字段（默认列之外）",
        options = extra_col_options(merged),
        default = [],
        key     = f"{key_prefix}_extra",
    )

    n_fold_rows = sum(b["count"] for b in blocks)
    n_visible = sum(1 for v in visible if v)
    st.caption(f"时间线共 {n} 条（登录+切换账号）。"
               + (f"默认折叠 {len(blocks)} 处、{n_fold_rows} 条相同设备连续登录。" if blocks else "")
               + "点表头排序、上方输入框筛选；点折叠条 ＋ 就地展开。")

    # 估算高度：常显元素 = 可见行 + 折叠条数；行高 ~30px，封顶 640
    rendered = n_visible + len(blocks)
    wrap_h = max(180, min(640, 40 + 30 * rendered))
    iframe_h = wrap_h + 96   # 留给筛选/排序控制条 + 边距

    components.html(
        _build_table_html(merged, visible, blocks, extra, wrap_h),
        height    = iframe_h,
        scrolling = False,
    )

    # 全量展示（用于 CSV 导出，恒为完整时间线）
    return build_display(merged, extra)
