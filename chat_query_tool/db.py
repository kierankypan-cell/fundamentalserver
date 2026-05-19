"""
db.py —— Kyuubi 查询封装

支持三种 engine（通过 KYUUBI_ENGINE_TYPE 切换）：
  - JDBC（默认，推荐）：Kyuubi 服务端已把 JDBC engine 默认路由到公司 Presto 集群，
    客户端只要声明 type=JDBC 即可，最快最稳；同事 kyuubi.py 走的就是这条
  - TRINO：Kyuubi 原生 Trino engine（需要客户端传 connection.url + user，比较麻烦）
  - SPARK_SQL：YARN 上拉 Spark 应用，启动慢；本工具走轻量配置

对外暴露：
  query_user_chats(roleid, zoneid, start_ymd, end_ymd) -> (country, df_out, df_in)
    - 三条 SQL 并发执行（country / out_battle / in_battle）
    - 全局最多 3 个用户同时查询（BoundedSemaphore）
  query_country / query_out_battle / query_in_battle      底层单条 SQL 函数
  active_user_count() -> int                              当前正在查询的用户数（UI 显示用）
  MAX_CONCURRENT_USERS                                    上限常量
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from pyhive import hive
from TCLIService.ttypes import TOperationState

# 兼容两种 .env 放置（同 analyzer.py）
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

# Trino engine 才需要这些（仅 KYUUBI_ENGINE_TYPE=TRINO 时生效）；JDBC 模式下服务端已默认配好
KYUUBI_TRINO_URL     = os.environ.get('KYUUBI_TRINO_URL',     'http://172.31.38.63:9088')
KYUUBI_TRINO_CATALOG = os.environ.get('KYUUBI_TRINO_CATALOG', 'hive')


# ============================================================
# 全局并发限流：最多 3 个用户同时查询
# ============================================================

MAX_CONCURRENT_USERS = int(os.environ.get('MAX_CONCURRENT_USERS', '3'))
_user_sem            = threading.BoundedSemaphore(MAX_CONCURRENT_USERS)
_active_count        = 0
_count_lock          = threading.Lock()


def active_user_count() -> int:
    """当前正在查询的用户数（用于 UI 排队提示）。"""
    with _count_lock:
        return _active_count


@contextmanager
def _acquire_user_slot(timeout: float = 2.0):
    """
    阻塞获取一个查询配额；带超时是为了让上层能在等待中刷新 UI。
    使用：
        while not slot.acquired():
            slot.try_acquire(timeout=2)
    这里直接用 contextmanager + 内层 while；外层 streamlit 通过包一层来更新 status。
    """
    global _active_count
    _user_sem.acquire()
    with _count_lock:
        _active_count += 1
    try:
        yield
    finally:
        with _count_lock:
            _active_count -= 1
        _user_sem.release()


def try_acquire_slot(timeout: float = 2.0) -> bool:
    """非阻塞尝试获取配额；成功返回 True，失败返回 False。需要配对调用 release_slot()。"""
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
        # JDBC engine：Kyuubi 服务端默认把 JDBC engine 路由到公司 Presto，
        # 客户端只声明类型即可，最简最稳。同事 kyuubi.py 用 SET 走的就是这条。
        return {
            'kyuubi.engine.type':                   'JDBC',
            'kyuubi.operation.incremental.collect': 'true',
        }

    if KYUUBI_ENGINE_TYPE == 'TRINO':
        # Trino engine（备用）：需要客户端传 Trino URL + 用户名；目前 Trino 端会要 user
        return {
            'kyuubi.engine.type':                             'TRINO',
            'kyuubi.session.engine.trino.connection.url':     KYUUBI_TRINO_URL,
            'kyuubi.session.engine.trino.connection.catalog': KYUUBI_TRINO_CATALOG,
            'kyuubi.operation.incremental.collect':           'true',
        }

    # 兜底回 SPARK_SQL（轻量配置：单玩家几千条聊天用不到大资源）
    return {
        'kyuubi.engine.type':                   'SPARK_SQL',
        'spark.yarn.queue':                     'adhoc_ml',
        'spark.sql.shuffle.partitions':         '200',     # 1000 → 200，单玩家不需要这么多分区
        'spark.dynamicAllocation.maxExecutors': '20',      # 100 → 20
        'spark.executor.cores':                 '2',       # 4 → 2
        'spark.executor.memory':                '4g',      # 14g → 4g
        'spark.executor.memoryOverhead':        '2g',      # 8g → 2g
        'kyuubi.operation.incremental.collect': 'true',
    }


# ============================================================
# 单条 SQL 执行
# ============================================================

def _run_sql(sql: str) -> pd.DataFrame:
    """执行一条 SQL 并返回 DataFrame。每次新建一个 connection（pyhive cursor 非线程安全）。"""
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
        time.sleep(0.5)              # 不再忙轮询，给 Kyuubi 减压
        status = cursor.poll().operationState

    if status == TOperationState.ERROR_STATE:
        raise RuntimeError(f'Kyuubi 查询失败：{cursor.poll().errorMessage}')

    cols = [d[0] for d in cursor.description]
    return pd.DataFrame(cursor.fetchall(), columns=cols)


# ============================================================
# 单条业务 SQL（保留作底层，向后兼容）
# ============================================================

def query_country(roleid: int, zoneid: int,
                  start_ymd: str, end_ymd: str) -> str | None:
    """通过最近一次登录的 client_ip 推断玩家国家（geoip 二字母代码）。"""
    sql = f"""
        SELECT geoip(client_ip, 1) AS country
        FROM ml_ods.gameserver_login
        WHERE logymd BETWEEN '{start_ymd}' AND '{end_ymd}'
          AND roleid = {int(roleid)}
          AND zoneid = {int(zoneid)}
          AND client_ip IS NOT NULL
        ORDER BY time DESC
        LIMIT 1
    """
    df = _run_sql(sql)
    if df.empty:
        return None
    val = df.iloc[0, 0]
    if val is None or pd.isna(val):
        return None
    s = str(val).strip().upper()
    return s or None


def query_out_battle(roleid: int, zoneid: int,
                     start_ymd: str, end_ymd: str) -> pd.DataFrame:
    """战斗外聊天（gameserver_chat_talk_v2 有 zoneid）。"""
    sql = f"""
        SELECT
            time,
            chat_type,
            chat_language,
            content,
            is_shield
        FROM ml_ods.gameserver_chat_talk_v2
        WHERE logymd BETWEEN '{start_ymd}' AND '{end_ymd}'
          AND zoneid = {int(zoneid)}
          AND sender = {int(roleid)}
        ORDER BY time
    """
    return _run_sql(sql)


def query_in_battle(roleid: int,
                    start_ymd: str, end_ymd: str) -> pd.DataFrame:
    """战斗内聊天（battleserver_chat_talk 无 zoneid）。"""
    sql = f"""
        SELECT
            time,
            chat_type,
            chat_language,
            content,
            is_shield
        FROM ml_ods.battleserver_chat_talk
        WHERE logymd BETWEEN '{start_ymd}' AND '{end_ymd}'
          AND sender = {int(roleid)}
        ORDER BY time
    """
    return _run_sql(sql)


# ============================================================
# 主入口：三条 SQL 并发 + 全局限流
# ============================================================

def query_user_chats(roleid: int, zoneid: int,
                     start_ymd: str, end_ymd: str
                     ) -> tuple[str | None, pd.DataFrame, pd.DataFrame]:
    """
    并发执行 country / out / in 三条 SQL，返回 (country, df_out, df_in)。

    调用方需先用 try_acquire_slot() / release_slot() 获取配额（让上层能控制 UI 排队提示）；
    或者直接用 with _acquire_user_slot() 包住调用。

    本函数本身不再 acquire，避免双重计数。
    """
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_country = ex.submit(query_country,    roleid, zoneid, start_ymd, end_ymd)
        f_out     = ex.submit(query_out_battle, roleid, zoneid, start_ymd, end_ymd)
        f_in      = ex.submit(query_in_battle,  roleid,         start_ymd, end_ymd)

        # 任何一个抛错都会冒泡上来；其它两个会被 ThreadPoolExecutor 退出时取消
        country = f_country.result()
        df_out  = f_out.result()
        df_in   = f_in.result()

    return country, df_out, df_in
