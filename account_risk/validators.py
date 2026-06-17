"""
validators.py —— 登录设备指纹校验（规则 4.1–4.5）

对每条登录日志做合法性校验，返回失败原因列表（空列表=通过）。纯函数、无副作用、
不依赖 streamlit / pandas 之外的东西，方便单测。

入口：
    validate_login(row: Mapping) -> list[str]
        row 可以是 DataFrame 的一行（dict / Series），字段名同 ml_ods.gameserver_login。

平台判定：deviceid 前缀决定走哪套规则
    and_ / and_test_ / and_taptest_  → 安卓（4.2 + 4.4）
    ios_ / ios_test_                  → iOS （4.3 + 4.5）
    其它前缀                          → 无法判定结构，记一条「deviceid 前缀无法识别」

os_type 语义（4.1）：1=iOS，2=安卓，3=编辑器，其它=异常。
编辑器(os_type=3)在安卓/iOS 两套里都算合法 os_type（device 形如 "system pc"）。
"""

from __future__ import annotations

import time
from typing import Mapping

ANDROID_PREFIXES = ("and_taptest_", "and_test_", "and_")   # 长前缀在前，避免误匹配
IOS_PREFIXES     = ("ios_test_", "ios_")

_EMPTY_TOKENS = {"", "none", "null", "nan", "(null)", "0", "none ", "<none>"}


# ============================================================
# 取值 / 空值辅助
# ============================================================

def _s(v) -> str:
    """转成去空白字符串；None / NaN → ''。"""
    if v is None:
        return ""
    try:
        # pandas NaN
        if v != v:          # NaN != NaN
            return ""
    except Exception:
        pass
    return str(v).strip()


def _is_empty(v) -> bool:
    """业务意义上的空：None / NaN / 空串 / 'none' / 'null' 等占位。"""
    return _s(v).lower() in _EMPTY_TOKENS


def _is_empty_str(v) -> bool:
    """仅判定字符串字段是否为空（不把 '0' 当空，用于 device/os_language 等文本字段）。"""
    return _s(v).lower() in {"", "none", "null", "nan", "(null)", "<none>"}


def _to_int(v):
    """尽力转 int；失败返回 None。"""
    s = _s(v)
    if s == "":
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _detect_platform(deviceid: str) -> str:
    """'android' / 'ios' / 'unknown'。"""
    d = deviceid
    if any(d.startswith(p) for p in ANDROID_PREFIXES):
        return "android"
    if any(d.startswith(p) for p in IOS_PREFIXES):
        return "ios"
    return "unknown"


def _android_prefix(deviceid: str) -> str | None:
    for p in ANDROID_PREFIXES:
        if deviceid.startswith(p):
            return p
    return None


def _ios_prefix(deviceid: str) -> str | None:
    for p in IOS_PREFIXES:
        if deviceid.startswith(p):
            return p
    return None


def _is_us(row: Mapping) -> bool:
    """美国注册或登录（用于 gpm_did 放过）。"""
    for f in ("createrole_country", "login_country"):
        if _s(row.get(f)).upper() in ("US", "USA"):
            return True
    return False


# ============================================================
# idfa 校验（4.3 / 4.5.3 共用）
# ============================================================

def check_idfa(idfa_raw) -> list[str]:
    """
    idfa 规则：
      - 含 4 个 '-'，去掉 '-' 后 32 位、仅数字字母（UUID 形如 8-4-4-4-12）。
      - 若含 '_'，其后跟 16 位纯数字时间戳（微秒），且不能是未来时间。
    """
    reasons: list[str] = []
    idfa = _s(idfa_raw)
    if _is_empty(idfa):
        return ["idfa 为空"]

    # 拆分可选的 _时间戳 后缀
    if "_" in idfa:
        uuid_part, _, ts_part = idfa.partition("_")
        if not ts_part.isdigit() or len(ts_part) != 16:
            reasons.append(f"idfa 时间戳后缀应为 16 位纯数字，实际 '{ts_part}'")
        else:
            # 16 位微秒时间戳 → 秒
            ts_sec = int(ts_part) / 1_000_000
            if ts_sec > time.time() + 60:        # 容忍 60s 时钟偏差
                reasons.append(f"idfa 时间戳为未来时间（{ts_part}）")
    else:
        uuid_part = idfa

    if uuid_part.count("-") != 4:
        reasons.append(f"idfa 应含 4 个 '-'，实际 {uuid_part.count('-')} 个")
    body = uuid_part.replace("-", "")
    if len(body) != 32:
        reasons.append(f"idfa 去掉 '-' 后应为 32 位，实际 {len(body)} 位")
    elif not body.isalnum():
        reasons.append("idfa 主体应仅含数字字母")
    return reasons


# ============================================================
# 安卓 deviceid 结构校验（4.2 / 4.4.7 共用）
# ============================================================

def check_android_deviceid(row: Mapping) -> list[str]:
    """
    安卓 deviceid 结构 = 前缀 + device_uniqueid + android_id + ad_id
      - android_id 必须 16 位
      - device_uniqueid 必须 32 位
      - ad_id 非空时必须 36 位且含 4 个 '-'
      - 拼接结果必须与 deviceid 完全一致
    """
    reasons: list[str] = []
    deviceid   = _s(row.get("deviceid"))
    uniqueid   = _s(row.get("device_uniqueid"))
    android_id = _s(row.get("android_id"))
    ad_id      = _s(row.get("ad_id"))

    prefix = _android_prefix(deviceid)
    if prefix is None:
        return ["deviceid 不以 and_/and_test_/and_taptest_ 开头"]

    if len(android_id) != 16:
        reasons.append(f"android_id 应为 16 位，实际 {len(android_id)} 位")
    if len(uniqueid) != 32:
        reasons.append(f"device_uniqueid 应为 32 位，实际 {len(uniqueid)} 位")

    ad_part = ""
    if not _is_empty(ad_id):
        ad_part = ad_id
        if len(ad_id) != 36:
            reasons.append(f"ad_id 应为 36 位，实际 {len(ad_id)} 位")
        if ad_id.count("-") != 4:
            reasons.append(f"ad_id 应含 4 个 '-'，实际 {ad_id.count('-')} 个")

    expected = prefix + uniqueid + android_id + ad_part
    if deviceid != expected:
        reasons.append("deviceid ≠ 前缀+device_uniqueid+android_id+ad_id（拼接不一致）")
    return reasons


# ============================================================
# 安卓整体校验（4.4）
# ============================================================

def _validate_android(row: Mapping, reasons: list[str]) -> None:
    # 重连记录（reconn=1）：client_memory=0 / os_version 为空属正常，忽略这两项校验
    is_reconn = _to_int(row.get("reconn")) == 1

    # 4.4.1 device 非空（编辑器形如 system pc，仍算非空）
    if _is_empty_str(row.get("device")):
        reasons.append("device 为空")
    # 4.4.2 os_type ∈ {2,3}
    ot = _to_int(row.get("os_type"))
    if ot not in (2, 3):
        reasons.append(f"安卓 os_type 应为 2 或 3，实际 {row.get('os_type')}")
    # 4.4.3 createrole_country 非空
    if _is_empty_str(row.get("createrole_country")):
        reasons.append("createrole_country 为空")
    # 4.4.4 os_language 非空
    if _is_empty_str(row.get("os_language")):
        reasons.append("os_language 为空")
    # 4.4.5 client_language ≠ 0
    if (_to_int(row.get("client_language")) or 0) == 0:
        reasons.append("client_language 为 0")
    # 4.4.6 client_memory ≠ 0（重连记录跳过）
    if not is_reconn and (_to_int(row.get("client_memory")) or 0) == 0:
        reasons.append("client_memory 为 0")
    # 4.4.7 deviceid 非空且结构正确
    if _is_empty(row.get("deviceid")):
        reasons.append("deviceid 为空")
    else:
        reasons.extend(check_android_deviceid(row))
    # 4.4.8 device_info 非空
    if _is_empty_str(row.get("device_info")):
        reasons.append("device_info 为空")
    # 4.4.9 os_version 非空（重连记录跳过）
    if not is_reconn and _is_empty_str(row.get("os_version")):
        reasons.append("os_version 为空")
    # 4.4.10 unity_version 非空
    if _is_empty_str(row.get("unity_version")):
        reasons.append("unity_version 为空")
    # 4.4.11 languageid ≠ 0
    if (_to_int(row.get("languageid")) or 0) == 0:
        reasons.append("languageid 为 0")
    # 4.4.12 gpm_did 非空（美国注册/登录放过）
    if _is_empty_str(row.get("gpm_did")) and not _is_us(row):
        reasons.append("gpm_did 为空（且非美国注册/登录）")
    # 4.4.13 packet_name 非空
    if _is_empty_str(row.get("packet_name")):
        reasons.append("packet_name 为空")
    # 4.4.14 login_country 非空
    if _is_empty_str(row.get("login_country")):
        reasons.append("login_country 为空")
    # 4.4.15 channel 以 and_ 开头
    if not _s(row.get("channel")).startswith("and_"):
        reasons.append(f"channel 应以 and_ 开头，实际 '{_s(row.get('channel'))}'")


# ============================================================
# iOS 整体校验（4.5）
# ============================================================

def _validate_ios(row: Mapping, reasons: list[str]) -> None:
    deviceid = _s(row.get("deviceid"))
    # 4.5.1 deviceid 以 ios_ 开头
    if _ios_prefix(deviceid) is None:
        reasons.append("deviceid 不以 ios_ 开头")
    # 4.5.2 os_type ∈ {1,3}
    ot = _to_int(row.get("os_type"))
    if ot not in (1, 3):
        reasons.append(f"iOS os_type 应为 1 或 3，实际 {row.get('os_type')}")
    # 4.5.3 idfa 校验（同时校验 deviceid = 前缀 + idfa）
    reasons.extend(check_idfa(row.get("idfa")))
    prefix = _ios_prefix(deviceid)
    if prefix is not None:
        idfa = _s(row.get("idfa"))
        if not _is_empty(idfa) and deviceid != prefix + idfa:
            reasons.append("deviceid ≠ 前缀+idfa（拼接不一致）")
    # 4.5.4 channel 以 ios_ 开头
    if not _s(row.get("channel")).startswith("ios_"):
        reasons.append(f"channel 应以 ios_ 开头，实际 '{_s(row.get('channel'))}'")


# ============================================================
# 主入口
# ============================================================

def validate_login(row: Mapping) -> list[str]:
    """对一行登录日志做全部校验，返回失败原因列表（空=通过）。"""
    reasons: list[str] = []

    # 4.1 os_type 合法性
    ot = _to_int(row.get("os_type"))
    if ot not in (1, 2, 3):
        reasons.append(f"os_type 异常（应为 1/2/3，实际 {row.get('os_type')}）")

    # 平台判定 → 走对应规则集
    deviceid = _s(row.get("deviceid"))
    platform = _detect_platform(deviceid)
    if platform == "android":
        _validate_android(row, reasons)
    elif platform == "ios":
        _validate_ios(row, reasons)
    else:
        # 无法按 deviceid 前缀判定平台，按 os_type 兜底提示
        if ot == 1:
            reasons.append("os_type=1(iOS) 但 deviceid 不以 ios_ 开头")
            _validate_ios(row, reasons)
        elif ot == 2:
            reasons.append("os_type=2(安卓) 但 deviceid 不以 and_ 开头")
            _validate_android(row, reasons)
        elif _is_empty(deviceid):
            reasons.append("deviceid 为空，无法判定平台")
        else:
            reasons.append(f"deviceid 前缀无法识别（非 and_/ios_）：'{deviceid[:24]}…'")

    # 去重保序
    seen = set()
    uniq = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq
