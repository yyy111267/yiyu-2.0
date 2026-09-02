# 以渔2.0 后端容器（python:3.11-slim，非 root 运行）
FROM python:3.11-slim AS base

# westock CLI 依赖 nodejs
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates sqlite3 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g westock-data-clawhub || true

# 非 root 用户
RUN useradd -m -u 1001 yiyu
WORKDIR /app
RUN chown yiyu:yiyu /app

# 代码与运行时 Markdown 资产（prompts / skills）必须先拷贝再构建。
COPY --chown=yiyu:yiyu . .
RUN pip install --no-cache-dir .

# 数据目录
RUN mkdir -p /app/data && chown yiyu:yiyu /app/data

USER yiyu
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/ready || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
