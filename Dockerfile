FROM python:3.11-slim

ARG APP_VERSION=0.4.0

LABEL org.opencontainers.image.title="file-manage-agent" \
    org.opencontainers.image.version="${APP_VERSION}" \
    org.opencontainers.image.description="支持确定性 Task Orchestration、人工恢复与安全 CLI 进度摘要的只读 LangGraph 文件版本治理 Agent"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system agent \
    && useradd --system --gid agent --create-home agent

COPY pyproject.toml README.md ./
COPY app ./app
COPY configs ./configs
COPY examples ./examples
# 受控 Prompt 是运行时资源，必须在安装和切换非 root 用户前复制到镜像。
COPY resources ./resources

RUN test -f /app/resources/prompts/file_governance_system_v1.md \
    && python -m pip install "." \
    && mkdir -p /data/input /data/artifacts/content \
        /data/artifacts/reports /data/artifacts/checkpoints /data/evidence \
    && chown -R agent:agent /data/input /data/artifacts /data/evidence

USER agent

VOLUME ["/data/input", "/data/artifacts", "/data/evidence"]

# 默认展示 CLI 帮助；实际运行时传入 run 或 resume 子命令及请求文件。
ENTRYPOINT ["file-governance"]
CMD ["--help"]
