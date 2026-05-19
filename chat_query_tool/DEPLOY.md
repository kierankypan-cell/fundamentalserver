# 部署文档

MLBB 玩家聊天风险查询工具（Streamlit + Kyuubi + Claude）部署指南。

涵盖三件事：
1. [通过 Kyuubi 连 Presto 的方式](#一通过-kyuubi-连-presto)
2. [容器化（Docker / docker-compose）](#二容器化)
3. [服务器部署步骤](#三服务器部署)

---

## 一、通过 Kyuubi 连 Presto

### 1.1 背景

公司数仓只暴露一个 Kyuubi 入口（`kyuubi.bi.moontontech.net:10009`），Kyuubi 后端可以路由到 Spark / Presto / Trino 等不同 engine。本工具的查询都是单玩家聊天（数据量小、要求低延迟），走 **Presto** 最合适 —— 比 Spark 快一个数量级，启动也快。

### 1.2 三种 engine 对比

| engine | 特点 | 是否推荐 |
|---|---|---|
| **JDBC**（推荐） | Kyuubi 服务端已默认把 JDBC engine 路由到公司 Presto 集群，客户端只要声明 `engine.type=JDBC` 即可 | ✅ 最简最稳 |
| TRINO | Kyuubi 原生 Trino engine，需要客户端额外传 `connection.url` + `user`，且 Trino 服务端会强制要求 `User must be set` | ❌ 配置复杂、易报错 |
| SPARK_SQL | YARN 上拉 Spark 应用，启动慢（30s+），单玩家几千条聊天用不到这种火力 | ❌ 慢，仅作回退 |

实测同事 `kyuubi.py` 跑通的方式就是在 SQL 前 `SET kyuubi.engine.type=JDBC;`，本质和我们 `configuration={'kyuubi.engine.type': 'JDBC'}` 是一回事。

### 1.3 关键代码

[db.py:110-139](db.py#L110-L139) 的 `_build_configuration()` 通过 `KYUUBI_ENGINE_TYPE` 环境变量切换 engine：

```python
if KYUUBI_ENGINE_TYPE == 'JDBC':
    return {
        'kyuubi.engine.type':                   'JDBC',
        'kyuubi.operation.incremental.collect': 'true',
    }
```

连接复用 `pyhive.hive.connect(..., auth='LDAP', configuration=...)`，每条 SQL 新建一个 cursor（pyhive 的 cursor 不是线程安全的）。

### 1.4 三个 SQL 并发执行

[db.py:243-264](db.py#L243-L264) 的 `query_user_chats()` 用 `ThreadPoolExecutor(max_workers=3)` 把 country / 战斗外 / 战斗内三条 SQL 同时投出去，总耗时 = max(3 SQL)，而不是 sum(3 SQL)。

### 1.5 多用户并发限流

[db.py:55-103](db.py#L55-L103) 用模块级 `BoundedSemaphore(MAX_CONCURRENT_USERS)` 卡总并发，避免多人同时点查询把 Kyuubi 打挂。前端通过 `try_acquire_slot()` / `release_slot()` 配对调用，超过上限的用户在 UI 上看到排队提示。

---

## 二、容器化

### 2.1 关键设计

| 设计点 | 选择 | 理由 |
|---|---|---|
| 基础镜像 | `python:3.11-slim` | ~50MB，比标准 python 镜像小 5x |
| Python 版本 | 3.11 | 3.10+ 删了 `longintrepr.h`，编译 `sasl` C 扩展会挂；改用 `pure-sasl` 纯 Python 实现绕过 |
| 不装 `sasl` 包 | 只装 `pyhive + thrift + thrift_sasl` | `thrift_sasl` 自动用 `pure-sasl`，不再依赖 C 扩展 |
| 镜像层缓存 | 先 `COPY requirements.txt`，再 `COPY .` | 只改 `.py` 时 pip install 那层命中缓存，rebuild 秒级 |
| 配置注入 | `env_file: .env` | `.env` 不进镜像（被 `.dockerignore` 排除），不进 git（被 `.gitignore` 排除），换机器只搬 `.env` |
| 历史持久化 | volume `./history:/app/history` | 容器删了重建，历史查询不丢；备份直接 `cp -r history/` |
| 重启策略 | `unless-stopped` | 崩溃 / docker daemon 重启会拉起，但 `docker stop` 后保持停止 |
| 日志限制 | `max-size: 10m, max-file: 3` | 默认无限增长会把磁盘吃满 |

### 2.2 关键文件

```
chat_query_tool/
├── Dockerfile             # 镜像构建
├── docker-compose.yaml    # 编排（端口/挂载/重启/日志）
├── requirements.txt       # Python 依赖（注意：没有 sasl）
├── .dockerignore          # 排除 .env / history / venv / __pycache__ / .git
├── .gitignore             # 排除 .env / history / venv / __pycache__
└── .env                   # 真实密钥（手动从 .env.example 复制后填）
```

### 2.3 系统依赖

[Dockerfile:15-21](Dockerfile#L15-L21) 装了 `build-essential` + `libsasl2-dev` 等，是为了让 `pip install` 阶段万一有源码包需要编译时不挂。`pure-sasl` 本身不需要这些，但留着兜底成本不高。

### 2.4 健康检查

[Dockerfile:45-47](Dockerfile#L45-L47) 用 streamlit 内置的 `/_stcore/health` 端点 30s 检查一次，`docker ps` 能直接看出来 `(healthy)` / `(unhealthy)`。

---

## 三、服务器部署

### 3.1 前置条件

服务器上需要：
- Docker + docker compose（v2 plugin，命令是 `docker compose` 不是 `docker-compose`）
- Git
- 可访问 `kyuubi.bi.moontontech.net:10009`（公司内网）
- 可访问 `llm.moontontech.net`（Claude 代理）

### 3.2 初次部署

```bash
# 1. 拉代码
git clone <repo-url> chat_query_tool
cd chat_query_tool

# 2. 准备 .env（从模板复制）
cp .env.example .env
vim .env
# 至少填：
#   KYUUBI_USER=<你的 LDAP>
#   KYUUBI_PASSWORD=<你的 LDAP 密码>
#   ANTHROPIC_API_KEY=<公司代理给的 key>
# KYUUBI_ENGINE_TYPE 默认 JDBC，不用改
# ANTHROPIC_BASE_URL 默认指公司代理，不用改

# 3. 起服务
docker compose up -d --build

# 4. 验证
docker compose ps                  # 看到 chat-audit 状态 Up + healthy
docker compose logs -f chat-audit  # 看启动日志
```

启动后访问 `http://<服务器 IP>:8501` 就是页面。

### 3.3 更新代码

```bash
git pull
docker compose up -d --build       # --build 重建镜像；不加 --build 只重启不会更新代码
```

### 3.4 常用运维命令

```bash
# 看日志（最近 100 行）
docker compose logs --tail=100 chat-audit

# 实时跟随日志
docker compose logs -f chat-audit

# 重启（不重建镜像）
docker compose restart chat-audit

# 进容器排查
docker exec -it chat-audit /bin/bash
# 容器里可以测网络：
#   nslookup kyuubi.bi.moontontech.net
#   curl -v -m 5 kyuubi.bi.moontontech.net:10009

# 停服务
docker compose down

# 看资源占用
docker stats chat-audit
```

### 3.5 历史数据备份

历史查询通过 volume 挂载到宿主机 `./history`，直接 cp 走即可：

```bash
tar czf history-backup-$(date +%Y%m%d).tar.gz history/
```

换服务器时把 `./history` 整个目录搬到新机器同样位置即可（`.env` 也要带）。

### 3.6 反代到 80 端口（可选）

如果想用 nginx 反代：

```bash
# docker-compose.yaml 里把 ports 改成只绑本机：
# ports:
#   - "127.0.0.1:8501:8501"
```

然后 nginx 配 `proxy_pass http://127.0.0.1:8501;`，注意 streamlit 走 websocket，需要：

```nginx
location / {
    proxy_pass http://127.0.0.1:8501;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 86400;
}
```

---

## 四、常见问题

### Q1：查询时报 `Caused by: java.net.SocketTimeoutException ... TqsAnalyzer`

Kyuubi 服务端调公司 TQS 鉴权服务超时。**重试 1-2 次**通常就好。`db.py` 已识别这个模式，UI 会直接提示用户重试。

如果稳定复现：
- 本机能跑、服务器不行 → 服务器到 Kyuubi 网络问题，找运维
- 本机也不行 → TQS 后端故障，找数仓团队

### Q2：`fatal error: longintrepr.h: No such file or directory`

`requirements.txt` 里装了 `sasl` 才会出。删掉它，靠 `thrift_sasl + pure-sasl`。

### Q3：docker compose 报 `.env` 解析失败

`docker compose` 的 env_file 比 `python-dotenv` 严格，**注释必须用 `#` 开头，不能有 `===` 之类的纯分割线**。如果本地 `.env` 是从注释丰富的模板改的，确保所有非 `KEY=VALUE` 行都以 `#` 开头。

### Q4：Streamlit 改了代码不生效

容器内是 `streamlit run`，不是 dev 模式，需要 `docker compose up -d --build` 重建镜像。

### Q5：超过 3 个用户同时查会怎样？

第 4 个开始排队，UI 会显示「⏳ 前方还有 N 人在查询，已等 Xs...」。上限通过 `MAX_CONCURRENT_USERS` 调，注意调高时也要看 Kyuubi 那边的承受能力。

---

## 五、文件速查

| 文件 | 作用 |
|---|---|
| [app.py](app.py) | Streamlit 主入口、UI、流程编排 |
| [db.py](db.py) | Kyuubi 查询封装、engine 切换、并发限流 |
| [analyzer.py](analyzer.py) | Claude 调用、批量分类、prompt cache |
| [prompts.py](prompts.py) | 分类器 system prompt |
| [history.py](history.py) | 历史查询持久化（按 roleid 索引） |
| [Dockerfile](Dockerfile) | 镜像构建 |
| [docker-compose.yaml](docker-compose.yaml) | 容器编排 |
| [.env.example](.env.example) | 配置模板，复制后填真实值 |
| [requirements.txt](requirements.txt) | Python 依赖 |
