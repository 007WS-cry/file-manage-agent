from __future__ import annotations

import json
import sysconfig
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.state.models import EmailMCPRecordState
from app.tools.email_mcp_client import (
    EMAIL_EVIDENCE_TOOL_NAME,
    normalize_email_mcp_record,
)

"""本模块从固定脱敏 JSON 数据创建只读 Streamable HTTP 模拟邮件 MCP 服务。"""


# 模拟邮件数据允许读取的最大字节数。
MAX_MOCK_EMAIL_DATA_BYTES = 5 * 1024 * 1024

# 当前模拟邮件数据文件接受的协议版本。
MOCK_EMAIL_SCHEMA_VERSION = "1.0"

def _resolve_default_mock_email_data_path() -> Path:
    """定位源码、容器或 wheel 数据目录中的公开模拟邮件数据。

    Returns:
        优先指向源码树示例，否则指向 setuptools data-files 安装位置的路径。
    """
    relative_path = Path("examples/mock_email_data.json")
    source_path = Path(__file__).resolve().parents[2] / relative_path
    if source_path.is_file():
        return source_path
    return Path(sysconfig.get_path("data")) / relative_path


# 源码、容器和 wheel 环境共享的公开脱敏模拟邮件数据路径。
DEFAULT_MOCK_EMAIL_DATA_PATH = _resolve_default_mock_email_data_path()


def load_mock_email_records(
    data_path: str | Path,
    *,
    max_bytes: int = MAX_MOCK_EMAIL_DATA_BYTES,
) -> list[EmailMCPRecordState]:
    """只读加载模拟服务公开的脱敏邮件附件证据。

    Args:
        data_path: 用户或容器明确提供的 UTF-8 JSON 普通文件。
        max_bytes: 允许读取的最大字节数。

    Returns:
        ID 唯一且已通过邮件 MCP 固定协议校验的记录。

    Raises:
        OSError: 文件不存在或无法读取时抛出。
        ValueError: 路径、大小、JSON 或记录协议不合法时抛出。
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes 必须是正整数")
    candidate = Path(data_path).expanduser()
    if candidate.is_symlink():
        raise ValueError("模拟邮件数据路径不得是符号链接")
    resolved_path = candidate.resolve(strict=True)
    if not resolved_path.is_file():
        raise ValueError("模拟邮件数据路径必须是普通文件")
    if resolved_path.stat().st_size > max_bytes:
        raise ValueError("模拟邮件数据超过读取上限")
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("模拟邮件数据必须是合法 UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("模拟邮件数据顶层必须是对象")
    if payload.get("schema_version") != MOCK_EMAIL_SCHEMA_VERSION:
        raise ValueError(f"模拟邮件 schema_version 必须为 {MOCK_EMAIL_SCHEMA_VERSION}")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise ValueError("模拟邮件数据 records 必须是数组")
    records = [
        normalize_email_mcp_record(value, index=index) for index, value in enumerate(raw_records)
    ]
    record_ids = [record["id"] for record in records]
    if len(set(record_ids)) != len(record_ids):
        raise ValueError("模拟邮件数据包含重复记录 ID")
    return records


def create_mock_email_mcp_server(
    data_path: str | Path = DEFAULT_MOCK_EMAIL_DATA_PATH,
    *,
    host: str = "127.0.0.1",
    port: int = 8001,
    log_level: str = "INFO",
) -> FastMCP:
    """创建只暴露脱敏附件查询 Tool 的无状态模拟邮件 MCP。

    Args:
        data_path: 启动时一次性只读加载的公开模拟数据文件。
        host: Streamable HTTP 服务监听地址。
        port: Streamable HTTP 服务监听端口。
        log_level: 官方 MCP 服务内部使用的日志级别。

    Returns:
        已注册唯一只读 Tool、但尚未启动网络监听的 FastMCP 服务。

    Raises:
        ValueError: 主机、端口、日志级别或模拟数据不合法时抛出。
    """
    normalized_host = host.strip() if isinstance(host, str) else ""
    if not normalized_host:
        raise ValueError("host 必须是非空字符串")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise ValueError("port 必须位于 1 到 65535 之间")
    normalized_level = log_level.strip().upper() if isinstance(log_level, str) else ""
    if normalized_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("log_level 必须是标准日志级别")
    records = load_mock_email_records(data_path)
    server = FastMCP(
        name="file-governance-mock-email",
        instructions=(
            "只读返回公开模拟数据中的脱敏附件发送和客户确认事实；"
            "不提供邮件正文、真实地址、发送、修改或删除能力。"
        ),
        host=normalized_host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        log_level=normalized_level,
    )

    @server.tool(name=EMAIL_EVIDENCE_TOOL_NAME)
    def search_sent_email_evidence(
        attachment_names: list[str],
        limit: int = 200,
    ) -> dict[str, list[dict[str, object]]]:
        """只读查询指定附件名的脱敏发送与客户确认事实。

        本工具仅返回启动时载入的模拟结构化元数据，不返回邮件正文、真实收件地址
        或附件内容，也不能发送、修改、移动、下载或删除任何邮件。

        Args:
            attachment_names: 只允许使用附件基础文件名进行精确、不区分大小写匹配。
            limit: 单次最多返回的记录数量，必须位于 1 到 500。

        Returns:
            固定 ``records`` 字段中的脱敏模拟邮件证据数组。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit 必须位于 1 到 500 之间")
        if not isinstance(attachment_names, list) or len(attachment_names) > 500:
            raise ValueError("attachment_names 必须是不超过 500 项的数组")
        normalized_names: set[str] = set()
        for index, name in enumerate(attachment_names):
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"attachment_names[{index}] 必须是非空字符串")
            normalized_name = name.strip()
            if (
                normalized_name in {".", ".."}
                or "/" in normalized_name
                or "\\" in normalized_name
                or Path(normalized_name).name != normalized_name
            ):
                raise ValueError(f"attachment_names[{index}] 必须是基础文件名")
            normalized_names.add(normalized_name.casefold())
        matched = [
            dict(record)
            for record in records
            if record["attachment_name"].casefold() in normalized_names
        ][:limit]
        return {"records": matched}

    return server
