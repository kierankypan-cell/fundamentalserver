# ─── 基础镜像 ──────────────────────────────────────────────────
# python:3.11-slim：~50MB，比标准 python 镜像小 5x；3.11 稳定且 streamlit 兼容
FROM python:3.11-slim

# ─── 元信息（可选）────────────────────────────────────────────
LABEL maintainer="bi-team"
LABEL description="MLBB 玩家聊天风险查询工具（Streamlit）"

# ─── 系统依赖 ─────────────────────────────────────────────────
# pyhive 走 LDAP 时依赖 sasl C 扩展，需要：
#   build-essential / gcc        编译 sasl / thrift_sasl 的 C 代码
#   libsasl2-dev + libsasl2-modules  SASL 头文件 + 运行时模块
#   libssl-dev                   SSL 头，部分驱动编译需要
# --no-install-recommends + rm apt cache，让镜像层尽量小
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libsasl2-dev \
        libsasl2-modules \
        libssl-dev && \
    rm -rf /var/lib/apt/lists/*

# ─── 工作目录 ─────────────────────────────────────────────────
# 后续 COPY / RUN / CMD 都以 /app 为基准；容器内 streamlit 启动时也在这里
WORKDIR /app

# ─── Python 依赖（先装，利用 Docker 层缓存）──────────────────
# 单独 COPY requirements.txt 而不是 COPY .，是为了：
#   只要 requirements.txt 没变，下面 pip install 那层就命中缓存，
#   改 .py 代码时重 build 只重跑 COPY . . 之后的层（秒级）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── 拷贝源码 ─────────────────────────────────────────────────
# .dockerignore 已排除 .env / venv / history / __pycache__ / .git
COPY . .

# ─── 端口 ────────────────────────────────────────────────────
# Streamlit 默认 8501；EXPOSE 是给读 Dockerfile 的人看的元数据，
# 真正暴露端口靠 docker run -p 或 docker-compose 的 ports
EXPOSE 8501

# ─── 健康检查（可选）──────────────────────────────────────────
# Streamlit 内置 /_stcore/health 端点；30s 一次
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        sys.exit(0) if urllib.request.urlopen('http://localhost:8501/_stcore/health').read() == b'ok' else sys.exit(1)"

# ─── 启动命令 ─────────────────────────────────────────────────
# --server.address=0.0.0.0  监听所有接口（默认只监听 localhost，容器外访问不到）
# --server.port=8501        端口
# --server.headless=true    禁用首启动浏览器和邮箱收集提示
# --browser.gatherUsageStats=false  关掉匿名遥测
CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
