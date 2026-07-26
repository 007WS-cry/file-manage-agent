from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from app.mcp_servers.mock_email import (
    DEFAULT_MOCK_EMAIL_DATA_PATH,
    create_mock_email_mcp_server,
)
from app.observability.logging import (
    configure_structured_logging,
    log_runtime_event,
)

"""本模块解析模拟邮件 MCP 参数，并用统一 JSON 日志启动 Streamable HTTP 服务。"""


# 模拟邮件 MCP 监听地址使用的环境变量名称。
MOCK_EMAIL_MCP_HOST_ENV = "FILE_GOVERNANCE_MOCK_EMAIL_MCP_HOST"

# 模拟邮件 MCP 监听端口使用的环境变量名称。
MOCK_EMAIL_MCP_PORT_ENV = "FILE_GOVERNANCE_MOCK_EMAIL_MCP_PORT"

# 模拟邮件 MCP 只读数据文件使用的环境变量名称。
MOCK_EMAIL_MCP_DATA_PATH_ENV = "FILE_GOVERNANCE_MOCK_EMAIL_MCP_DATA_PATH"

# 四类运行时服务共享的 JSON 日志级别环境变量名称。
LOG_LEVEL_ENV = "FILE_GOVERNANCE_LOG_LEVEL"


def build_argument_parser() -> argparse.ArgumentParser:
    """构建模拟邮件 MCP 进程的命令行参数解析器。

    Returns:
        包含数据文件、监听地址、端口和日志级别的解析器。
    """
    parser = argparse.ArgumentParser(
        prog="file-governance-mock-email-mcp",
        description="启动只读脱敏邮件附件证据的 Streamable HTTP 模拟 MCP。",
    )
    parser.add_argument(
        "--data-path",
        type=Path,
        default=Path(
            os.environ.get(
                MOCK_EMAIL_MCP_DATA_PATH_ENV,
                str(DEFAULT_MOCK_EMAIL_DATA_PATH),
            )
        ),
        help="启动时一次性加载的 UTF-8 脱敏模拟邮件 JSON 文件。",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get(MOCK_EMAIL_MCP_HOST_ENV, "127.0.0.1"),
        help="Streamable HTTP MCP 监听地址。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get(MOCK_EMAIL_MCP_PORT_ENV, "8001")),
        help="Streamable HTTP MCP 监听端口。",
    )
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug"),
        default=os.environ.get(LOG_LEVEL_ENV, "INFO").lower(),
        help="模拟 MCP、Uvicorn 与 SDK 统一使用的 JSON 日志级别。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """启动只读模拟邮件 MCP，并阻塞到 Uvicorn 安全关闭。

    Args:
        argv: 可选命令行参数序列；省略时读取当前进程参数。

    Returns:
        服务正常关闭后返回零。
    """
    arguments = build_argument_parser().parse_args(argv)
    logger = configure_structured_logging(
        "mock_email_mcp",
        level=arguments.log_level,
    )
    server = create_mock_email_mcp_server(
        arguments.data_path,
        host=arguments.host,
        port=arguments.port,
        log_level=arguments.log_level,
    )
    log_runtime_event(logger, "service_starting", "模拟邮件 MCP 正在启动。")
    uvicorn.run(
        server.streamable_http_app(),
        host=arguments.host,
        port=arguments.port,
        log_level=arguments.log_level,
        log_config=None,
    )
    log_runtime_event(logger, "service_stopped", "模拟邮件 MCP 已安全停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
