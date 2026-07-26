from __future__ import annotations

import json
from pathlib import Path

from app.graphs.evidence import evidence_graph
from tests.integration.test_email_mcp_evidence import make_evidence_state

"""本文件集成测试邮件 MCP 不可用时自动切换本地发送日志的非阻断证据链。"""


def test_unavailable_email_mcp_falls_back_to_local_delivery_log(
    tmp_path: Path,
) -> None:
    """连接失败应留下 mcp 错误事实，并用本地日志生成 DeliveryRecord。"""
    local_log_path = tmp_path / "delivery_log.json"
    local_log_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "deliveries": [
                    {
                        "id": "local-delivery-001",
                        "attachment_name": "contract-v3.docx",
                        "attachment_sha256": "a" * 64,
                        "normalized_digest": None,
                        "sent_at": "2026-07-18T09:30:00+08:00",
                        "recipient_label": "customer-A",
                        "customer_confirmed": True,
                        "evidence_ref": "local-log://delivery-001",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state = make_evidence_state("http://127.0.0.1:1/mcp")
    state["email_mcp"]["timeout_seconds"] = 0.1
    state["request"]["delivery_log_path"] = str(local_log_path)

    result = evidence_graph.invoke(state)

    assert result["email_mcp_fetch"]["status"] == "fallback"
    assert result["email_mcp_fetch"]["fallback_used"] is True
    assert result["deliveries"][0]["evidence_source"] == "local_log"
    assert result["deliveries"][0]["file_id"] == "file-contract-v3"
    assert any(
        error["category"] == "mcp"
        and error["node_name"] == "load_email_mcp_evidence"
        for error in result["errors"]
    )
