FROM python:3.11-slim

ARG APP_VERSION=1.0.2
ARG LLM_EXTRAS=

LABEL org.opencontainers.image.title="file-manage-agent" \
    org.opencontainers.image.version="${APP_VERSION}" \
    org.opencontainers.image.description="具备语义变更分析、确定性人工审核规则与双数据库部署能力的只读文件版本治理 Agent"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    FILE_GOVERNANCE_DATABASE_URL= \
    FILE_GOVERNANCE_DATABASE_PATH=/data/artifacts/database/file-governance-app.sqlite3 \
    FILE_GOVERNANCE_CHECKPOINT_PATH=/data/artifacts/checkpoints/file-governance-background.sqlite3 \
    FILE_GOVERNANCE_API_HOST=0.0.0.0 \
    FILE_GOVERNANCE_API_PORT=8000 \
    FILE_GOVERNANCE_LOG_LEVEL=INFO \
    FILE_GOVERNANCE_EMAIL_MCP_ENABLED=false \
    FILE_GOVERNANCE_EMAIL_MCP_URL=http://127.0.0.1:8001/mcp \
    FILE_GOVERNANCE_MOCK_EMAIL_MCP_HOST=0.0.0.0 \
    FILE_GOVERNANCE_MOCK_EMAIL_MCP_PORT=8001 \
    FILE_GOVERNANCE_MOCK_EMAIL_MCP_DATA_PATH=/app/examples/mock_email_data.json

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system agent \
    && useradd --system --gid agent --create-home agent

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY configs ./configs
COPY examples ./examples
COPY scripts ./scripts
# 受控 Prompt 是运行时资源，必须在安装和切换非 root 用户前复制到镜像。
COPY resources ./resources

RUN test -f /app/resources/prompts/file_governance_system_v1.md \
    && test -f /app/resources/skills/registry.yaml \
    && test -f /app/resources/skills/file-content-analysis/SKILL.md \
    && test -f /app/resources/skills/version-relation/SKILL.md \
    && test -f /app/resources/skills/evidence-confidence/SKILL.md \
    && test -f /app/resources/skills/governance-report/SKILL.md \
    && if [ -n "${LLM_EXTRAS}" ]; then \
        python -m pip install ".[${LLM_EXTRAS}]"; \
    else \
        python -m pip install "."; \
    fi \
    && mkdir -p /data/input /data/artifacts/content \
        /data/artifacts/reports /data/artifacts/checkpoints \
        /data/artifacts/database /data/artifacts/worktrees /data/evidence \
    && chown -R agent:agent /data/input /data/artifacts /data/evidence

USER agent

VOLUME ["/data/input", "/data/artifacts", "/data/evidence"]

EXPOSE 8000 8001

# 1.0.2 默认启动 HTTP API；Worker、Scheduler 或模拟邮件 MCP 可通过命令参数整体覆盖。
CMD ["file-governance-api", "--host", "0.0.0.0", "--port", "8000"]
