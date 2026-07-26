from __future__ import annotations

import asyncio

import pytest

from app.state.factories import create_email_mcp_config_state
from app.tools.email_mcp_client import (
    fetch_email_mcp_evidence_async,
    normalize_email_mcp_record,
)

"""本文件单元测试邮件 MCP 客户端的固定协议校验与关闭状态网络隔离。"""


def make_email_record() -> dict[str, object]:
    """构造不包含正文、真实地址或凭据的合法邮件 MCP 记录。

    Returns:
        可以通过客户端协议校验的脱敏字典。
    """
    return {
        "id": "email-evidence-001",
        "attachment_name": "contract-v3.docx",
        "attachment_sha256": "a" * 64,
        "normalized_digest": None,
        "sent_at": "2026-07-18T09:30:00+08:00",
        "recipient_label": "customer-A",
        "customer_confirmed": True,
        "evidence_ref": "email-mcp://mock/thread-001/attachment-001",
    }


def test_normalize_email_mcp_record_accepts_fixed_protocol() -> None:
    """合法脱敏记录应规范化摘要并保持固定证据引用。"""
    record = normalize_email_mcp_record(make_email_record(), index=0)

    assert record["attachment_name"] == "contract-v3.docx"
    assert record["attachment_sha256"] == "a" * 64
    assert record["customer_confirmed"] is True
    assert record["evidence_ref"].startswith("email-mcp://")


def test_normalize_email_mcp_record_rejects_non_mcp_reference() -> None:
    """伪造本地或 HTTP 引用不得进入邮件 MCP 证据状态。"""
    payload = make_email_record()
    payload["evidence_ref"] = "https://mail.example/messages/001"

    with pytest.raises(ValueError, match="email-mcp://"):
        normalize_email_mcp_record(payload, index=0)


def test_normalize_email_mcp_record_rejects_attachment_path() -> None:
    """服务端不得用目录路径伪装附件基础文件名。"""
    payload = make_email_record()
    payload["attachment_name"] = "../contract-v3.docx"

    with pytest.raises(ValueError, match="基础文件名"):
        normalize_email_mcp_record(payload, index=0)


def test_disabled_email_mcp_client_does_not_open_network() -> None:
    """关闭配置应在创建 HTTP 或 MCP 会话前直接返回空记录。"""
    config = create_email_mcp_config_state(
        {
            "enabled": False,
            "server_url": "http://127.0.0.1:1/mcp",
            "timeout_seconds": 0.1,
            "max_results": 10,
        }
    )

    records = asyncio.run(
        fetch_email_mcp_evidence_async(config, ["contract-v3.docx"])
    )

    assert records == []
