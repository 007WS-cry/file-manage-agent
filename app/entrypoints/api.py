from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from app.api.app import (
    APPLICATION_DATABASE_PATH_ENV,
    CHECKPOINT_PATH_ENV,
    DEFAULT_RUNTIME_CHECKPOINT_PATH,
    create_app,
)
from app.observability.logging import (
    configure_structured_logging,
    log_runtime_event,
)
from app.storage.database import DEFAULT_APPLICATION_DATABASE_PATH

"""本模块解析 HTTP API 进程参数并启动只暴露脱敏运行状态的 Uvicorn 服务。"""


# API 监听地址使用的环境变量名称。
API_HOST_ENV = "FILE_GOVERNANCE_API_HOST"

# API 监听端口使用的环境变量名称。
API_PORT_ENV = "FILE_GOVERNANCE_API_PORT"

# 四类运行时服务共享的 JSON 日志级别环境变量名称。
LOG_LEVEL_ENV = "FILE_GOVERNANCE_LOG_LEVEL"


def build_argument_parser() -> argparse.ArgumentParser:
    """构建 HTTP API 进程的命令行参数解析器。

    Returns:
        包含监听地址、端口、数据库和 checkpoint 路径的解析器。
    """
    parser = argparse.ArgumentParser(
        prog="file-governance-api",
        description="启动文件版本治理后台提交和状态查询 HTTP API。",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get(API_HOST_ENV, "127.0.0.1"),
        help="API 监听地址，默认只监听本机回环地址。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get(API_PORT_ENV, "8000")),
        help="API 监听端口。",
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=Path(
            os.environ.get(
                APPLICATION_DATABASE_PATH_ENV,
                str(DEFAULT_APPLICATION_DATABASE_PATH),
            )
        ),
        help="已执行 Alembic 迁移的应用数据库路径。",
    )
    parser.add_argument(
        "--checkpoint-path",
        type=Path,
        default=Path(
            os.environ.get(
                CHECKPOINT_PATH_ENV,
                str(DEFAULT_RUNTIME_CHECKPOINT_PATH),
            )
        ),
        help="API 和 Worker 共用的 SQLite checkpoint 路径。",
    )
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug"),
        default=os.environ.get(LOG_LEVEL_ENV, "INFO").lower(),
        help="API 与 Uvicorn 统一 JSON 日志级别。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """启动 HTTP API 并阻塞到 Uvicorn 服务关闭。

    Args:
        argv: 可选命令行参数序列；省略时读取当前进程参数。

    Returns:
        Uvicorn 正常关闭后返回零。
    """
    arguments = build_argument_parser().parse_args(argv)
    if arguments.port < 1 or arguments.port > 65535:
        raise ValueError("port 必须位于 1 到 65535 之间")
    logger = configure_structured_logging("api", level=arguments.log_level)
    log_runtime_event(logger, "service_starting", "HTTP API 正在启动。")
    application = create_app(
        database_path=arguments.database_path,
        checkpoint_path=arguments.checkpoint_path,
    )
    uvicorn.run(
        application,
        host=arguments.host,
        port=arguments.port,
        log_level=arguments.log_level,
        log_config=None,
    )
    log_runtime_event(logger, "service_stopped", "HTTP API 已安全停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
