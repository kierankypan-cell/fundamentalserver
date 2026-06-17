# ─── 基础镜像 ──────────────────────────────────────────────────
FROM python:3.11-slim

LABEL maintainer="bi-team"
LABEL description="MLBB 玩家登录/账号风险查询工具（Streamlit）"

# ─── 系统依赖 ─────────────────────────────────────────────────
# pyhive 走 LDAP 时依赖 SASL：build-essential 编译，libsasl2-* 提供头文件+模块
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libsasl2-dev \
        libsasl2-modules \
        libssl-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─── Python 依赖（先装，利用层缓存）──────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── 拷贝源码 ─────────────────────────────────────────────────
COPY . .

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        sys.exit(0) if urllib.request.urlopen('http://localhost:8501/_stcore/health').read() == b'ok' else sys.exit(1)"

CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
