# syntax=docker/dockerfile:1.7
# ============================================================================
# Maestro Dockerfile — Python 3.13 slim
#
# 设计要点：
# - 单阶段构建（小项目不值得多阶段，构建时间 < 镜像大小 优势更大）
# - 非 root 用户（容器逃逸安全）
# - tini 初始化（正确处理 SIGTERM / zombie 回收）
# - 健康检查指向 /api/healthz
# - 不复制 .env（运行时通过 env_file 注入；密钥不入镜像）
#
# 构建：docker build -t maestro:latest .
# 运行：docker run -d -p 8787:8787 --env-file .env -v maestro-data:/app/data maestro:latest
# ============================================================================

FROM python:3.13-slim AS base

# 防止 Python 写 .pyc + stdout buffering（容器日志实时输出）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tini 优雅处理信号（uvicorn 不收 SIGTERM 的话进程会卡 10 秒）
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 非 root 用户（容器逃逸后无 root 权限）
RUN groupadd --system --gid 1000 maestro \
    && useradd --system --uid 1000 --gid maestro --create-home --shell /bin/bash maestro

WORKDIR /app

# 先装依赖（缓存友好：依赖文件不变就不重装）
COPY --chown=maestro:maestro requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码（.dockerignore 排除 .env、outputs、__pycache__ 等）
COPY --chown=maestro:maestro src ./src
COPY --chown=maestro:maestro web ./web
COPY --chown=maestro:maestro wallpaper ./wallpaper
COPY --chown=maestro:maestro configs ./configs

# 数据目录（运行时挂卷用）
RUN mkdir -p /app/data /app/outputs && chown -R maestro:maestro /app

USER maestro

# 健康检查（每 30 秒打 /api/healthz，连续3 次失败视为不健康）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/healthz').status == 200 else 1)"

EXPOSE 8787

# tini 作为 PID 1（SIGTERM 转发 + zombie 回收）
ENTRYPOINT ["/usr/bin/tini", "--"]

# 启动 uvicorn（workers=1；多 worker 需先支持多 db 模式，超出 P1 范围）
CMD ["python", "-m", "uvicorn", "maestro.server:app", \
     "--host", "0.0.0.0", \
     "--port", "8787", \
     "--workers", "1", \
     "--log-level", "info", \
     "--no-access-log"]   # access 日志交给我们的 logging（避免重复输出）