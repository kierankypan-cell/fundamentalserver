"""
db.py —— Kyuubi 查询封装（account_risk 登录/账号风险工具）

连接/限流骨架与 chat_query_tool/db.py 完全一致（pyhive 连 Kyuubi，LDAP，
默认 JDBC engine 由服务端路由到公司 Presto），只替换业务 SQL。

对外暴露：
  query_login(roleid, zoneid, start_ymd, end_ymd) -> DataFrame
      该 roleid 在时间范围内的全部登录日志，附带 geoip 城市(geo_city)与网络供应商(geo_isp)。
      **不硬过滤 zoneid**：一个 roleid 可跨多区，硬卡会漏；由上层 UI 标记非目标区。
  query_rebind(roleid, start_ymd, end_ymd) -> DataFrame
      该 roleid 作为「新账号(new_accountid)」的全部切换账号(rebind)记录。
  query_login_and_rebind(...) -> (df_login, df_rebind)   两条 SQL 并发
  try_acquire_slot / release_slot / active_user_count / MAX_CONCURRENT_USERS  全局并发限流
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from pyhive import hive
from TCLIService.ttypes import TOperationState

# 兼容两种 .env 放置：本目录优先，其次项目根（根 .env 已含 KYUUBI_*）
for _env_path in [Path(__file__).parent / '.env',
                  Path(__file__).parent.parent / '.env']:
    if _env_path.exists():
        load_dotenv(_env_path)
        break

KYUUBI_HOST        = os.environ.get('KYUUBI_HOST', 'kyuubi.bi.moontontech.net')
KYUUBI_PORT        = os.environ.get('KYUUBI_PORT', '10009')
KYUUBI_USER        = os.environ.get('KYUUBI_USER')
KYUUBI_PASSWORD    = os.environ.get('KYUUBI_PASSWORD')
KYUUBI_ENGINE_TYPE = os.environ.get('KYUUBI_ENGINE_TYPE', 'JDBC').upper()

KYUUBI_TRINO_URL     = os.environ.get('KYUUBI_TRINO_URL',     'http://172.31.38.63:9088')
KYUUBI_TRINO_CATALOG = os.environ.get('KYUUBI_TRINO_CATALOG', 'hive')


# ============================================================
# 全局并发限流：最多 N 个用户同时查询
# ============================================================

MAX_CONCURRENT_USERS = int(os.environ.get('MAX_CONCURRENT_USERS', '3'))
_user_sem            = threading.BoundedSemaphore(MAX_CONCURRENT_USERS)
_active_count        = 0
_count_lock          = threading.Lock()


def active_user_count() -> int:
    """当前正在查询的用户数（用于 UI 排队提示）。"""
    with _count_lock:
        return _active_count


def try_acquire_slot(timeout: float = 2.0) -> bool:
    """非阻塞尝试获取配额；成功返回 True。需配对调用 release_slot()。"""
    global _active_count
    if _user_sem.acquire(timeout=timeout):
        with _count_lock:
            _active_count += 1
        return True
    return False


def release_slot() -> None:
    """释放配额（与 try_acquire_slot 配对）。"""
    global _active_count
    with _count_lock:
        _active_count -= 1
    _user_sem.release()


# ============================================================
# Kyuubi 配置 profile
# ============================================================

def _build_configuration() -> dict:
    """根据 KYUUBI_ENGINE_TYPE 选择不同的 engine + 资源配置。"""
    if KYUUBI_ENGINE_TYPE == 'JDBC':
        return {
            'kyuubi.engine.type':                   'JDBC',
            'kyuubi.operation.incremental.collect': 'true',
        }

    if KYUUBI_ENGINE_TYPE == 'TRINO':
        return {
            'kyuubi.engine.type':                             'TRINO',
            'kyuubi.session.engine.trino.connection.url':     KYUUBI_TRINO_URL,
            'kyuubi.session.engine.trino.connection.catalog': KYUUBI_TRINO_CATALOG,
            'kyuubi.operation.incremental.collect':           'true',
        }

    return {
        'kyuubi.engine.type':                   'SPARK_SQL',
        'spark.yarn.queue':                     'adhoc_ml',
        'spark.sql.shuffle.partitions':         '200',
        'spark.dynamicAllocation.maxExecutors': '20',
        'spark.executor.cores':                 '2',
        'spark.executor.memory':                '4g',
        'spark.executor.memoryOverhead':        '2g',
        'kyuubi.operation.incremental.collect': 'true',
    }


# ============================================================
# 错误信息整理
# ============================================================

def _format_kyuubi_error(raw: str | None) -> str:
    """从整坨 Java 堆栈里抽出 root cause，对已知模式给中文提示。"""
    if not raw:
        return '未知错误（Kyuubi 返回空 errorMessage）'

    causes = re.findall(r'Caused by:\s*([^\n]+)', raw)
    root = (causes[-1] if causes else raw.splitlines()[0]).strip()

    if len(root) > 400:
        root = root[:400] + ' ...(已截断)'

    if 'SocketTimeoutException' in raw and 'TqsAnalyzer' in raw:
        return f'Kyuubi 鉴权服务（TQS）超时，通常重试 1-2 次即可。\n（原始：{root}）'
    if 'not registered' in root or 'Function not found' in root:
        return f'SQL 函数在当前 engine 不可用：{root}'
    if 'AuthenticationException' in raw or 'LDAP' in raw:
        return f'Kyuubi 鉴权失败，检查 .env 的 KYUUBI_USER / KYUUBI_PASSWORD：{root}'

    return root


# ============================================================
# 单条 SQL 执行
# ============================================================

def _run_sql(sql: str) -> pd.DataFrame:
    """执行一条 SQL 并返回 DataFrame。每次新建 connection（pyhive cursor 非线程安全）。"""
    if not KYUUBI_USER or not KYUUBI_PASSWORD:
        raise RuntimeError(
            'KYUUBI_USER / KYUUBI_PASSWORD 未在 .env 中配置，请补全后重试。'
        )

    cursor = hive.connect(
        host          = KYUUBI_HOST,
        port          = KYUUBI_PORT,
        username      = KYUUBI_USER,
        password      = KYUUBI_PASSWORD,
        auth          = 'LDAP',
        configuration = _build_configuration(),
    ).cursor()

    cursor.execute(sql, async_=True)
    status = cursor.poll().operationState
    while status in (TOperationState.INITIALIZED_STATE, TOperationState.RUNNING_STATE):
        time.sleep(0.5)
        status = cursor.poll().operationState

    if status == TOperationState.ERROR_STATE:
        raise RuntimeError(_format_kyuubi_error(cursor.poll().errorMessage))

    cols = [d[0] for d in cursor.description]
    return pd.DataFrame(cursor.fetchall(), columns=cols)


# ============================================================
# 业务 SQL
# ============================================================

def query_login(roleid: int, zoneid: int,
                start_ymd: str, end_ymd: str) -> pd.DataFrame:
    """
    拉取该 roleid 在时间范围内的全部登录日志，附带 geoip 城市/网络供应商两列。

    **不在 SQL 里硬过滤 zoneid**：一个 roleid 可在多个区有角色，硬卡 zoneid 会把别区登录
    全部漏掉（甚至直接 0 条）；这里只按 roleid 拉、把 zoneid 一并取出，由上层 UI 标记
    「非目标区服」。（与 chat_query_tool 的健壮性策略一致。）

    geoip(client_ip,3) → "国家 / 地区 / 城市"；geoip(client_ip,5) → 网络供应商。
    Presto 端 NULL client_ip 时 geoip 返回空，不影响整行。
    """
    sql = f"""
        SELECT
            *,
            geoip(client_ip, 3) AS geo_city,
            geoip(client_ip, 5) AS geo_isp
        FROM ml_ods.gameserver_login
        WHERE logymd BETWEEN '{start_ymd}' AND '{end_ymd}'
          AND roleid = {int(roleid)}
        ORDER BY time
    """
    return _run_sql(sql)


def query_rebind(roleid: int, start_ymd: str, end_ymd: str) -> pd.DataFrame:
    """
    切换账号(rebind)：该 roleid 作为「绑定后的新账号 new_accountid」的记录。

    accountserver_account_rebind 无 zoneid 维度，按 new_accountid 过滤即可定位。
    异常登录检测需要「自创角以来」的全部 rebind，因此 start_ymd 通常传创角日，
    范围可能跨数年（按天分区，单账号过滤，几秒~几十秒）。
    """
    sql = f"""
        SELECT *
        FROM ml_ods.accountserver_account_rebind
        WHERE logymd BETWEEN '{start_ymd}' AND '{end_ymd}'
          AND new_accountid = {int(roleid)}
        ORDER BY time
    """
    return _run_sql(sql)


def query_create_device(roleid: int, create_ymd: str) -> str | None:
    """
    返回玩家「创角设备」的 deviceid：创角日 create_ymd 当天该 roleid 最早一条登录的 deviceid。

    create_role 表不含 deviceid（只有 device 机型 / uniqueid），故按需求绕道 login 表：
    用创角日做分区裁剪 + roleid 过滤，取最早登录的 deviceid 作为创角设备。
    创角日登录数据已过期（分区被清）时返回 None，由上层降级处理。
    """
    sql = f"""
        SELECT deviceid
        FROM ml_ods.gameserver_login
        WHERE logymd = '{create_ymd}'
          AND roleid = {int(roleid)}
          AND deviceid IS NOT NULL
        ORDER BY time
        LIMIT 1
    """
    df = _run_sql(sql)
    if df.empty:
        return None
    v = df.iloc[0, 0]
    if v is None or pd.isna(v):
        return None
    s = str(v).strip()
    return s or None


def query_aux(roleid: int, create_ymd: str | None,
              rebind_start_ymd: str, rebind_end_ymd: str
              ) -> tuple[str | None, pd.DataFrame]:
    """
    并发拉取异常登录检测所需的辅助数据：(创角设备 deviceid, 自创角以来的全部 rebind)。

    调用方需自行 try_acquire_slot() / release_slot() 管理配额（与 query_login 共用一个配额）。
    """
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_dev = (ex.submit(query_create_device, roleid, create_ymd)
                 if create_ymd else None)
        f_rb  = ex.submit(query_rebind, roleid, rebind_start_ymd, rebind_end_ymd)
        create_device = f_dev.result() if f_dev is not None else None
        df_rebind     = f_rb.result()
    return create_device, df_rebind
