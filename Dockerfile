FROM python:3.11-slim

ARG APP_VERSION=0.5.4
ARG LLM_EXTRAS=

LABEL org.opencontainers.image.title="file-manage-agent" \
    org.opencontainers.image.version="${APP_VERSION}" \
    org.opencontainers.image.description="支持安全短期/长期 Memory、Task 级 Skills、固定 Agent Team、LangChain 多模型路由、独立应用数据库迁移与 checkpoint 的只读文件版本治理 Agent"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    FILE_GOVERNANCE_DATABASE_PATH=/data/artifacts/database/file-governance-app.sqlite3

WORKDIR /app

RUN groupadd --system agent \
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
        /data/artifacts/database /data/evidence \
    && chown -R agent:agent /data/input /data/artifacts /data/evidence

USER agent

VOLUME ["/data/input", "/data/artifacts", "/data/evidence"]

# 默认展示 CLI 帮助；实际运行时传入 run 或 resume 子命令及请求文件。
ENTRYPOINT ["file-governance"]
CMD ["--help"]
