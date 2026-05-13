"""
db.py —— Kyuubi 查询封装

参考了 c:\\Users\\admin\\Downloads\\kyuubi.py 的连接方式，
但凭据从 .env 读取，避免硬编码。

对外暴露两个函数：
    query_out_battle(roleid, zoneid, start_ymd, end_ymd) -> pd.DataFrame
    query_in_battle(roleid, start_ymd, end_ymd) -> pd.DataFrame
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from pyhive import hive
from TCLIService.ttypes import TOperationState

# 兼容两种 .env 放置：本目录（chat_query_tool/.env，独立仓库 / Docker）
# 或上一级（pythondemo/.env，老布局）。Docker 里 docker-compose 直接注入
# env vars，找不到 .env 也无影响。
for _env_path in [Path(__file__).parent / '.env',
                  Path(__file__).parent.parent / '.env']:
    if _env_path.exists():
        load_dotenv(_env_path)
        break

KYUUBI_HOST     = os.environ.get('KYUUBI_HOST', 'kyuubi.bi.moontontech.net')
KYUUBI_PORT     = os.environ.get('KYUUBI_PORT', '10009')
KYUUBI_USER     = os.environ.get('KYUUBI_USER')
KYUUBI_PASSWORD = os.environ.get('KYUUBI_PASSWORD')


def _run_sql(sql: str) -> pd.DataFrame:
    """执行一条 SQL 并返回 DataFrame。"""
    if not KYUUBI_USER or not KYUUBI_PASSWORD:
        raise RuntimeError(
            'KYUUBI_USER / KYUUBI_PASSWORD 未在 .env 中配置，请补全后重试。'
        )

    configuration = {
        'kyuubi.engine.type':                  'SPARK_SQL',
        'spark.yarn.queue':                    'adhoc_ml',
        'spark.sql.shuffle.partitions':        '1000',
        'spark.dynamicAllocation.maxExecutors':'100',
        'spark.executor.cores':                '4',
        'spark.executor.memory':               '14g',
        'spark.executor.memoryOverhead':       '8g',
        'kyuubi.operation.incremental.collect':'true',
    }

    cursor = hive.connect(
        host          = KYUUBI_HOST,
        port          = KYUUBI_PORT,
        username      = KYUUBI_USER,
        password      = KYUUBI_PASSWORD,
        auth          = 'LDAP',
        configuration = configuration,
    ).cursor()

    cursor.execute(sql, async_=True)
    status = cursor.poll().operationState
    while status in (TOperationState.INITIALIZED_STATE, TOperationState.RUNNING_STATE):
        status = cursor.poll().operationState

    if status == TOperationState.ERROR_STATE:
        raise RuntimeError(f'Kyuubi 查询失败：{cursor.poll().errorMessage}')

    cols = [d[0] for d in cursor.description]
    return pd.DataFrame(cursor.fetchall(), columns=cols)


def query_country(roleid: int, zoneid: int,
                  start_ymd: str, end_ymd: str) -> str | None:
    """
    通过该玩家最近一次登录的 client_ip 推断所在国家（geoip 二字母代码）。
    没找到登录记录时返回 None。
    """
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
    """
    查询玩家在指定时间段的【战斗外】聊天记录。
    战斗外表 ml_ods.gameserver_chat_talk_v2 有 zoneid 字段。
    """
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
    """
    查询玩家在指定时间段的【战斗内】聊天记录。
    战斗内表 ml_ods.battleserver_chat_talk 没有 zoneid 字段。
    """
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
