# 聊天打点表字段说明

游戏内聊天数据分两张表存储，**战斗内**和**战斗外**。两张表都按日期分区（`logymd`，YYYY-MM-DD 字符串）。

> **关键字段速查**
> - `sender`：发送消息的玩家 ID（用作"谁说的话"过滤条件）
> - `is_shield`：是否被屏蔽。`1` = 被屏蔽（其他玩家看不到），`0` = 未屏蔽（其他玩家可见）
> - `zoneid`：分区 ID。**只在战斗外表有**，战斗内表无此字段
> - `content`：聊天文本内容（可能是中文 / 英文 / 印尼语 / 越南语等多语种）
> - `chat_language`：聊天频道（不是语种）

---

## 表 1：`ml_ods.gameserver_chat_talk_v2` —— 战斗外聊天

玩家在大厅、公会、好友、世界频道、私聊等**非战斗场景**的聊天打点。包含完整的玩家上下文信息（等级、充值、IP、机型等）。

| 序号 | 字段 | 类型 | 描述 |
|---|---|---|---|
| 1 | time | timestamp | 日志时间 |
| 2 | loghms | string | 日志时间 hms |
| 3 | **roleid** | decimal(20,0) | 角色 ID（日志归属角色） |
| 4 | **zoneid** | bigint | **分区 ID** |
| 5 | usercreatetime | timestamp | 创角时间 |
| 6 | usercreateymd | string | 创角时间 ymd |
| 7 | usercreatehms | string | 创角时间 hms |
| 8 | level | bigint | 玩家等级 |
| 9 | activedays | bigint | 累计登录天数 |
| 10 | device | string | 机型 |
| 11 | os_type | bigint | 创角 OS 类型 |
| 12 | channel | string | 渠道 |
| 13 | chargediamond | bigint | 充值钻石数量 |
| 14 | createrole_country | string | 创角国家 |
| 15 | client_ip | string | 客户端 IP |
| 16 | sex | bigint | 性别 |
| 17 | curdiamond | bigint | 当前钻石 |
| 18 | curbattle_points | bigint | 当前战点 |
| 19 | curticket | bigint | 当前点券 |
| 20 | chat_type | bigint | 聊天类型 |
| 21 | chat_tag | string | 类型标志 |
| 22 | **sender** | decimal(20,0) | **发送者** |
| 23 | target | decimal(20,0) | 目标 |
| 24 | battle_id | decimal(20,0) | 战斗 ID |
| 25 | team_id | decimal(20,0) | 工会 ID |
| 26 | room_id | decimal(20,0) | 房间 ID |
| 27 | chat_language | bigint | 聊天频道 |
| 28 | **content** | string | **内容** |
| 29 | circle_id | decimal(20,0) | 圈子 ID |
| 30 | **is_shield** | bigint | **是否被屏蔽** |
| 31 | model | bigint | 模型 ID |
| 32 | room_key | string | 房间号 |

**典型违规**：广告引流、色情/约炮诱导、诈骗、辱骂

---

## 表 2：`ml_ods.battleserver_chat_talk` —— 战斗内聊天

玩家在**对局/匹配**过程中的实时聊天。字段精简，**没有 zoneid**。

| 序号 | 字段 | 类型 | 描述 |
|---|---|---|---|
| 1 | time | timestamp | 日志时间 |
| 2 | loghms | string | 日志时间 hms |
| 3 | chat_type | bigint | 日志类型 |
| 4 | chat_tag | string | 类型标志 |
| 5 | **sender** | decimal(20,0) | **发送者** |
| 6 | target | decimal(20,0) | 目标 |
| 7 | battle_id | decimal(20,0) | 战斗 ID |
| 8 | team_id | bigint | 工会 ID |
| 9 | room_id | bigint | 房间 ID |
| 10 | chat_language | bigint | 聊天频道 |
| 11 | **content** | string | **内容** |
| 12 | battlesvrid | bigint | 战斗服 ID |
| 13 | sub_tag | bigint | 子类型 |
| 14 | **is_shield** | bigint | **是否被屏蔽** |
| 15 | model | bigint | 模型 ID |
| 16 | match_parm_chat_talk | string | 局内聊天参数 |
| 17 | oper_chat_type | bigint | 客户端 ChatType |

**典型违规**：辱骂队友/对手、嘲讽、人身攻击、仇恨言论、歧视

---

## 标准查询模板

### 查询某玩家战斗外聊天
```sql
SELECT time, chat_type, chat_language, content, is_shield
FROM ml_ods.gameserver_chat_talk_v2
WHERE logymd BETWEEN '2026-05-01' AND '2026-05-08'
  AND zoneid = 4001
  AND sender = 8677905
ORDER BY time;
```

### 查询某玩家战斗内聊天
```sql
SELECT time, chat_type, chat_language, content, is_shield
FROM ml_ods.battleserver_chat_talk
WHERE logymd BETWEEN '2026-05-01' AND '2026-05-08'
  AND sender = 8677905
ORDER BY time;
```

> 战斗内表没有 zoneid 字段，无法按区过滤。`sender` 应为全局唯一玩家 ID，可直接定位玩家。
