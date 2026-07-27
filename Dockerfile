# ── 构建层：装依赖到独立虚拟环境 ─────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# 先只拷贝依赖声明，充分利用镜像层缓存（代码改动不触发重装依赖）
COPY pyproject.toml ./
COPY app ./app

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install .

# ── 运行层：slim 基础镜像 + 非 root 用户 ────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd -r agentgate && useradd -r -g agentgate agentgate

WORKDIR /srv/agentgate

COPY --from=builder /opt/venv /opt/venv
# 运行所需：应用代码、迁移（dev 自动迁移 / 生产手动 upgrade）、播种脚本
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

USER agentgate

EXPOSE 8000

# 存活探针：/healthz 不依赖外部组件；就绪判断走 compose 的 /readyz
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
