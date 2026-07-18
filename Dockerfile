FROM python:3.11-slim

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

RUN python -m pip install "." \
    && mkdir -p /data/input /data/artifacts/content \
        /data/artifacts/reports /data/artifacts/checkpoints \
    && chown -R agent:agent /data/input /data/artifacts

USER agent

VOLUME ["/data/input", "/data/artifacts"]

# 默认展示 CLI 帮助；实际运行时传入 run 或 resume 子命令及请求文件。
ENTRYPOINT ["file-governance"]
CMD ["--help"]
