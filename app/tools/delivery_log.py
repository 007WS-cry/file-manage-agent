from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.state.models import DeliveryLogEntry

"""本模块以只读方式加载并严格校验本地客户发送记录 JSON。"""


# 本地发送记录允许读取的最大字节数，防止异常日志占用过多内存。
MAX_DELIVERY_LOG_BYTES = 10 * 1024 * 1024
# 当前接受的本地发送记录协议版本。
SUPPORTED_DELIVERY_LOG_SCHEMA_VERSION = "1.0"
# 用于校验 SHA-256 和标准化内容摘要的十六进制格式。
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def _require_non_empty_string(value: Any, *, field_name: str, index: int) -> str:
    """校验发送记录中的必填非空字符串字段。

    Args:
        value: 等待校验的 JSON 字段值。
        field_name: 用于错误信息的字段名称。
        index: 当前记录在 deliveries 数组中的下标。

    Returns:
        去除首尾空白后的字符串。

    Raises:
        ValueError: 字段不是非空字符串时抛出。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"deliveries[{index}].{field_name} 必须是非空字符串")
    return value.strip()


def _normalize_optional_digest(
    value: Any,
    *,
    field_name: str,
    index: int,
) -> str | None:
    """校验并规范化可选 SHA-256 摘要字段。

    Args:
        value: 等待校验的摘要值或 None。
        field_name: 用于错误信息的字段名称。
        index: 当前记录在 deliveries 数组中的下标。

    Returns:
        小写十六进制摘要；字段为空时返回 None。

    Raises:
        ValueError: 字段不是合法 SHA-256 摘要时抛出。
    """
    if value is None:
        return None
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value.strip()):
        raise ValueError(f"deliveries[{index}].{field_name} 必须是 64 位十六进制摘要")
    return value.strip().lower()


def _normalize_optional_sent_at(value: Any, *, index: int) -> str | None:
    """校验可选发送时间并要求时间字符串包含时区。

    Args:
        value: ISO 8601 发送时间字符串或 None。
        index: 当前记录在 deliveries 数组中的下标。

    Returns:
        原始 ISO 8601 字符串；字段为空时返回 None。

    Raises:
        ValueError: 字段不是带时区的合法 ISO 8601 时间时抛出。
    """
    if value is None:
        return None
    sent_at = _require_non_empty_string(value, field_name="sent_at", index=index)
    try:
        parsed = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"deliveries[{index}].sent_at 必须是合法 ISO 8601 时间"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError(f"deliveries[{index}].sent_at 必须包含时区")
    return sent_at


def _parse_delivery_entry(value: Any, *, index: int) -> DeliveryLogEntry:
    """把一条 JSON 对象转换为经过校验的发送日志状态。

    Args:
        value: deliveries 数组中的单条 JSON 值。
        index: 当前记录在 deliveries 数组中的下标。

    Returns:
        字段完整且已规范化的 ``DeliveryLogEntry``。

    Raises:
        ValueError: 记录或任一字段不符合协议时抛出。
    """
    if not isinstance(value, dict):
        raise ValueError(f"deliveries[{index}] 必须是对象")
    customer_confirmed = value.get("customer_confirmed")
    if not isinstance(customer_confirmed, bool):
        raise ValueError(f"deliveries[{index}].customer_confirmed 必须是布尔值")
    return DeliveryLogEntry(
        id=_require_non_empty_string(value.get("id"), field_name="id", index=index),
        attachment_name=_require_non_empty_string(
            value.get("attachment_name"),
            field_name="attachment_name",
            index=index,
        ),
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
        recipient_label=_require_non_empty_string(
            value.get("recipient_label"),
            field_name="recipient_label",
            index=index,
        ),
        customer_confirmed=customer_confirmed,
        evidence_ref=_require_non_empty_string(
            value.get("evidence_ref"),
            field_name="evidence_ref",
            index=index,
        ),
    )


def load_local_delivery_log(
    path: str | Path,
    *,
    max_bytes: int = MAX_DELIVERY_LOG_BYTES,
) -> list[DeliveryLogEntry]:
    """只读加载受信任路径中的本地发送记录 JSON。

    该工具只读取一个普通 UTF-8 JSON 文件，不访问网络、不打开附件、不执行
    日志内容，也不会创建、修改或删除任何文件。为避免越权读取和资源耗尽，
    工具拒绝符号链接、非普通文件、超限文件和不符合固定协议的数据。

    Args:
        path: 用户明确提供的本地发送记录 JSON 文件路径。
        max_bytes: 允许读取的最大字节数，必须大于零。

    Returns:
        保持输入顺序的、经过严格校验的发送记录列表。

    Raises:
        FileNotFoundError: 路径不存在时抛出。
        IsADirectoryError: 路径不是普通文件时抛出。
        OSError: 文件元数据或内容无法读取时抛出。
        ValueError: 路径为符号链接、文件超限、JSON 或协议不合法时抛出。
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须大于零")
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError("本地发送记录路径不得是符号链接")
    resolved_path = candidate.resolve(strict=True)
    if not resolved_path.is_file():
        raise IsADirectoryError(f"本地发送记录路径不是普通文件：{resolved_path}")
    if resolved_path.stat().st_size > max_bytes:
        raise ValueError(f"本地发送记录超过 {max_bytes} 字节读取上限")
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("本地发送记录必须是合法 UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("本地发送记录顶层必须是对象")
    if payload.get("schema_version") != SUPPORTED_DELIVERY_LOG_SCHEMA_VERSION:
        raise ValueError(
            "本地发送记录 schema_version 必须为 "
            f"{SUPPORTED_DELIVERY_LOG_SCHEMA_VERSION}"
        )
    raw_entries = payload.get("deliveries")
    if not isinstance(raw_entries, list):
        raise ValueError("本地发送记录 deliveries 必须是数组")
    entries = [
        _parse_delivery_entry(raw_entry, index=index)
        for index, raw_entry in enumerate(raw_entries)
    ]
    entry_ids = [entry["id"] for entry in entries]
    if len(set(entry_ids)) != len(entry_ids):
        raise ValueError("本地发送记录中存在重复 id")
    return entries
