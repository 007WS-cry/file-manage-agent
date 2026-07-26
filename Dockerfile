FROM python:3.11-slim

ARG APP_VERSION=0.7.2
ARG LLM_EXTRAS=

LABEL org.opencontainers.image.title="file-manage-agent" \
    org.opencontainers.image.version="${APP_VERSION}" \
    org.opencontainers.image.description="支持后台队列、Cron Scheduler、隔离 Worktree 与十表审计的文件版本治理 Agent"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    FILE_GOVERNANCE_DATABASE_PATH=/data/artifacts/database/file-governance-app.sqlite3 \
    FILE_GOVERNANCE_CHECKPOINT_PATH=/data/artifacts/checkpoints/file-governance-background.sqlite3 \
    FILE_GOVERNANCE_API_HOST=0.0.0.0 \
    FILE_GOVERNANCE_API_PORT=8000

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

EXPOSE 8000

# 默认启动 HTTP API；CLI、Worker 或 Scheduler 可通过 docker run 的命令参数整体覆盖。
CMD ["file-governance-api", "--host", "0.0.0.0", "--port", "8000"]
