# account_risk —— 玩家登录 / 账号风险查询工具

输入玩家 `roleid + zoneid + 时间范围`，工具会：

1. 拉取该 roleid 在时间范围内的全部 `ml_ods.gameserver_login` 登录日志，并用
   `geoip(client_ip,3)` / `geoip(client_ip,5)` 附带**城市**与**网络供应商**。
2. 对每条登录做**设备指纹合法性校验**（规则 4.1–4.5），失败行标红并列出原因。
3. 默认按 `(deviceid, device)` **折叠连续相同登录**（只留首尾，中间折叠可展开）。
4. 查 `ml_ods.accountserver_account_rebind`（`new_accountid = roleid`）的**切换账号**记录，
   按时间**插入登录时间线**（蓝色行），另设页签查看全部切换账号。

> 纯规则校验、**不调用任何 LLM**。只依赖 Kyuubi 数据仓库。

## 目录结构

```
account_risk/
├── app.py            Streamlit 主入口（表单 + 两个页签 + 侧边栏历史）
├── db.py             Kyuubi 连接 + query_login / query_rebind（并发 + 全局限流）
├── validators.py     登录设备指纹校验规则 4.1–4.5
├── timeline.py       合并 login + rebind 时间线 + 折叠可见性计算
├── ui_helpers.py     展示格式化 / 列选择 / 行高亮 / 折叠渲染
├── history.py        查询历史落盘（最多 20 条）
├── requirements.txt
├── Dockerfile
├── docker-compose.yaml
└── .env.example
```

## 本地运行

```bash
cd account_risk
cp .env.example .env          # 填上 KYUUBI_USER / KYUUBI_PASSWORD（或复用项目根 ../.env）
pip install -r requirements.txt
streamlit run app.py          # 默认 http://localhost:8501
```

`db.py` 会优先读本目录 `.env`，缺失时回退读项目根 `../.env`（项目根 .env 已含 `KYUUBI_*`）。

## Docker 部署

```bash
cd account_risk
cp .env.example .env          # 填上真实凭据
docker compose up -d --build  # 默认映射宿主机 8502 → 容器 8501
docker compose logs -f
```

> 与 `chat_query_tool`（8501）同机部署时，本服务宿主机端口默认用 **8502**，避免冲突。
> 查询历史落盘在 `./history`（compose 已挂 volume，容器重建不丢）。

## 校验规则速查（validators.py）

| 规则 | 说明 |
|------|------|
| 4.1 | `os_type` ∈ {1=iOS, 2=安卓, 3=编辑器}，其它异常 |
| 4.2 | 安卓 `deviceid` = 前缀(`and_`/`and_test_`/`and_taptest_`) + `device_uniqueid`(32位) + `android_id`(16位) + `ad_id`(非空时36位含4个`-`) |
| 4.3 | `idfa` 含4个`-`、去`-`后32位字母数字；若含`_`则其后16位时间戳须非未来时间 |
| 4.4 | 安卓字段完整性（device / createrole_country / os_language / client_language≠0 / client_memory≠0 / deviceid结构 / device_info / os_version / unity_version / languageid≠0 / gpm_did(美国放过) / packet_name / login_country / channel以`and_`开头） |
| 4.5 | iOS：deviceid以`ios_`开头、os_type∈{1,3}、idfa同4.3、channel以`ios_`开头 |
