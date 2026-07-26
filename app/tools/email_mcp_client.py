from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.state.models import EmailMCPConfigState, EmailMCPRecordState

"""本模块通过受控 Streamable HTTP MCP 会话只读查询脱敏邮件附件证据。"""


# 模拟邮件 MCP 对外提供的唯一只读证据工具名称。
EMAIL_EVIDENCE_TOOL_NAME = "search_sent_email_evidence"

# 单次 MCP 请求允许发送的最大附件名称数量。
MAX_ATTACHMENT_QUERY_NAMES = 500

# 邮件证据中 SHA-256 与标准化摘要允许使用的十六进制格式。
EMAIL_DIGEST_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def _require_string(value: object, *, field_name: str, index: int) -> str:
    """校验邮件 MCP 记录中的必填有限字符串。

    Args:
        value: 等待校验的 MCP 结构化字段。
        field_name: 当前字段名称。
        index: 当前记录在返回数组中的下标。

    Returns:
        去除首尾空白后的字符串。

    Raises:
        ValueError: 字段不是非空有限字符串时抛出。
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 1_000:
        raise ValueError(f"邮件 MCP records[{index}].{field_name} 必须是有限非空字符串")
    return value.strip()


def _normalize_optional_digest(
    value: object,
    *,
    field_name: str,
    index: int,
) -> str | None:
    """校验邮件 MCP 记录中的可选十六进制摘要。

    Args:
        value: None 或等待校验的摘要。
        field_name: 当前摘要字段名称。
        index: 当前记录在返回数组中的下标。

    Returns:
        规范化为小写的摘要；输入为 None 时返回 None。

    Raises:
        ValueError: 摘要不是 64 位十六进制字符串时抛出。
    """
    if value is None:
        return None
    if not isinstance(value, str) or not EMAIL_DIGEST_PATTERN.fullmatch(value.strip()):
        raise ValueError(f"邮件 MCP records[{index}].{field_name} 必须是 64 位十六进制摘要")
    return value.strip().lower()


def _normalize_optional_sent_at(value: object, *, index: int) -> str | None:
    """校验邮件 MCP 记录中的可选带时区发送时间。

    Args:
        value: None 或 ISO 8601 时间字符串。
        index: 当前记录在返回数组中的下标。

    Returns:
        原始带时区时间字符串；输入为 None 时返回 None。

    Raises:
        ValueError: 时间无法解析或不含时区时抛出。
    """
    if value is None:
        return None
    normalized = _require_string(value, field_name="sent_at", index=index)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"邮件 MCP records[{index}].sent_at 必须是 ISO 8601 时间") from error
    if parsed.tzinfo is None:
        raise ValueError(f"邮件 MCP records[{index}].sent_at 必须包含时区")
    return normalized


def normalize_email_mcp_record(
    value: object,
    *,
    index: int,
) -> EmailMCPRecordState:
    """把服务端数据或 MCP 返回对象规范化为脱敏邮件附件证据状态。

    本函数不访问网络、不读取附件或邮件正文，也不会发送、修改或删除邮件。
    它只允许固定结构化字段，并要求证据引用使用 ``email-mcp://`` 命名空间。

    Args:
        value: 等待校验的单条结构化记录。
        index: 记录在当前数组中的下标。

    Returns:
        可交给 Evidence 确定性匹配服务的邮件 MCP 记录。

    Raises:
        ValueError: 对象、摘要、时间、布尔值或证据引用不符合协议时抛出。
    """
    if not isinstance(value, dict):
        raise ValueError(f"邮件 MCP records[{index}] 必须是对象")
    customer_confirmed = value.get("customer_confirmed")
    if not isinstance(customer_confirmed, bool):
        raise ValueError(f"邮件 MCP records[{index}].customer_confirmed 必须是布尔值")
    attachment_name = _require_string(
        value.get("attachment_name"),
        field_name="attachment_name",
        index=index,
    )
    if (
        attachment_name in {".", ".."}
        or "/" in attachment_name
        or "\\" in attachment_name
        or Path(attachment_name).name != attachment_name
    ):
        raise ValueError(f"邮件 MCP records[{index}].attachment_name 必须是基础文件名")
    evidence_ref = _require_string(
        value.get("evidence_ref"),
        field_name="evidence_ref",
        index=index,
    )
    if not evidence_ref.startswith("email-mcp://"):
        raise ValueError(f"邮件 MCP records[{index}].evidence_ref 必须使用 email-mcp://")
    return EmailMCPRecordState(
        id=_require_string(value.get("id"), field_name="id", index=index),
        attachment_name=attachment_name,
        attachment_sha256=_normalize_optional_digest(
            value.get("attachment_sha256"),
            field_name="attachment_sha256",
            index=index,
        ),
        normalized_digest=_normalize_optional_digest(
            value.get("normalized_digest"),
            field_name="normalized_digest",
            index=index,
        ),
        sent_at=_normalize_optional_sent_at(value.get("sent_at"), index=index),
        recipient_label=_require_string(
            value.get("recipient_label"),
            field_name="recipient_label",
            index=index,
        ),
        customer_confirmed=customer_confirmed,
        evidence_ref=evidence_ref,
    )


def _extract_tool_records(result: object) -> list[object]:
    """从官方 MCP CallToolResult 的结构化或文本内容提取 records 数组。

    Args:
        result: ``ClientSession.call_tool`` 返回的协议对象。

    Returns:
        尚未执行字段级校验的记录数组。

    Raises:
        RuntimeError: Tool 返回错误或没有固定 records 对象时抛出。
    """
    if bool(getattr(result, "isError", False)):
        raise RuntimeError("邮件 MCP 只读证据工具返回错误")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and isinstance(structured.get("records"), list):
        return list(structured["records"])
    for content in getattr(result, "content", []):
        text = getattr(content, "text", None)
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("records"), list):
            return list(payload["records"])
    raise RuntimeError("邮件 MCP 工具没有返回固定 records 结构")


def _normalize_attachment_names(attachment_names: list[str]) -> list[str]:
    """规范化客户端允许提交给邮件 MCP 的附件基础文件名。

    Args:
        attachment_names: 当前治理状态中已发现文件的名称。

    Returns:
        去重并保持稳定顺序的有限基础文件名列表。

    Raises:
        ValueError: 名称数量越界或包含空值时抛出。
    """
    if len(attachment_names) > MAX_ATTACHMENT_QUERY_NAMES:
        raise ValueError(f"邮件 MCP 单次最多查询 {MAX_ATTACHMENT_QUERY_NAMES} 个附件名称")
    normalized: list[str] = []
    for index, value in enumerate(attachment_names):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"attachment_names[{index}] 必须是非空字符串")
        file_name = value.strip()
        if (
            file_name in {".", ".."}
            or "/" in file_name
            or "\\" in file_name
            or Path(file_name).name != file_name
        ):
            raise ValueError(f"attachment_names[{index}] 必须是基础文件名")
        if len(file_name) > 512:
            raise ValueError(f"attachment_names[{index}] 超过 512 字符")
        if file_name not in normalized:
            normalized.append(file_name)
    return normalized


async def fetch_email_mcp_evidence_async(
    config: EmailMCPConfigState,
    attachment_names: list[str],
) -> list[EmailMCPRecordState]:
    """通过官方 MCP 客户端只读查询当前文件集合的邮件发送证据。

    本工具只调用固定名称 ``search_sent_email_evidence``，只传递附件基础文件名和
    有限结果数量；不会请求邮件正文、凭据或真实地址，也不具备发送、修改、删除
    邮件或调用其他 MCP Tool 的能力。

    Args:
        config: 已由状态工厂校验的 MCP URL、超时和结果上限。
        attachment_names: 当前治理运行已发现的附件基础文件名。

    Returns:
        经过固定协议校验、去重且不超过 max_results 的脱敏证据记录。

    Raises:
        RuntimeError: MCP 会话、工具发现、调用或响应协议失败时抛出。
        ValueError: 配置、附件名称或返回记录越过安全边界时抛出。
    """
    if not config["enabled"]:
        return []
    parsed_url = urlsplit(config["server_url"])
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError("邮件 MCP server_url 必须是合法 HTTP(S) 端点")
    names = _normalize_attachment_names(attachment_names)
    timeout = float(config["timeout_seconds"])
    limits = int(config["max_results"])
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
    ) as http_client:
        async with streamable_http_client(
            config["server_url"],
            http_client=http_client,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                if EMAIL_EVIDENCE_TOOL_NAME not in {tool.name for tool in tools.tools}:
                    raise RuntimeError("邮件 MCP 未提供固定只读证据工具")
                result = await session.call_tool(
                    EMAIL_EVIDENCE_TOOL_NAME,
                    arguments={
                        "attachment_names": names,
                        "limit": limits,
                    },
                    read_timeout_seconds=timedelta(seconds=timeout),
                )
    raw_records = _extract_tool_records(result)
    if len(raw_records) > limits:
        raise ValueError("邮件 MCP 返回记录数量超过请求上限")
    records = [
        normalize_email_mcp_record(value, index=index) for index, value in enumerate(raw_records)
    ]
    record_ids = [record["id"] for record in records]
    if len(set(record_ids)) != len(record_ids):
        raise ValueError("邮件 MCP 返回重复证据 ID")
    return records


def fetch_email_mcp_evidence(
    config: EmailMCPConfigState,
    attachment_names: list[str],
) -> list[EmailMCPRecordState]:
    """同步执行一次受控邮件 MCP 只读证据查询。

    该包装只供同步 LangGraph Evidence 节点使用，不暴露任意 Tool 名称或参数。
    调用方处于异步事件循环时应改用 ``fetch_email_mcp_evidence_async``。

    Args:
        config: 已完成状态工厂校验的邮件 MCP 配置。
        attachment_names: 当前治理运行已发现的附件基础文件名。

    Returns:
        可直接进入确定性证据匹配的脱敏邮件记录。

    Raises:
        RuntimeError: 当前线程已有运行中的事件循环时抛出。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(fetch_email_mcp_evidence_async(config, attachment_names))
    raise RuntimeError("同步邮件 MCP 客户端不能在运行中的异步事件循环内调用")
