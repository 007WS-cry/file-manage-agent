from __future__ import annotations

import json
from pathlib import Path

from app.graphs.evidence import evidence_graph
from tests.integration.test_email_mcp_evidence import (
    make_evidence_state,
    run_mock_email_mcp,
)

"""本文件端到端验收真实邮件 MCP 成功取证与不可用时本地发送日志降级。"""


def test_mcp_success_and_local_log_fallback_are_both_explainable(
    tmp_path: Path,
) -> None:
    """同一附件应在 MCP 成功和连接失败时分别留下明确证据来源。"""
    mcp_data_path = tmp_path / "mock-email.json"
    mcp_data_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "records": [
                    {
                        "id": "e2e-email-001",
                        "attachment_name": "contract-v3.docx",
                        "attachment_sha256": "a" * 64,
                        "normalized_digest": None,
                        "sent_at": "2026-07-27T09:00:00+08:00",
                        "recipient_label": "demo-customer",
                        "customer_confirmed": True,
                        "evidence_ref": "email-mcp://demo/thread-001/attachment-001",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with run_mock_email_mcp(mcp_data_path) as server_url:
        mcp_result = evidence_graph.invoke(make_evidence_state(server_url))

    assert mcp_result["email_mcp_fetch"]["status"] == "available"
    assert mcp_result["email_mcp_fetch"]["fallback_used"] is False
    assert mcp_result["deliveries"][0]["evidence_source"] == "email_mcp"
    assert mcp_result["deliveries"][0]["customer_confirmed"] is True

    local_log_path = tmp_path / "delivery-log.json"
    local_log_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "deliveries": [
                    {
                        "id": "e2e-local-001",
                        "attachment_name": "contract-v3.docx",
                        "attachment_sha256": "a" * 64,
                        "normalized_digest": None,
                        "sent_at": "2026-07-27T09:00:00+08:00",
                        "recipient_label": "demo-customer",
                        "customer_confirmed": True,
                        "evidence_ref": "local-log://demo/delivery-001",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fallback_state = make_evidence_state("http://127.0.0.1:1/mcp")
    fallback_state["email_mcp"]["timeout_seconds"] = 0.1
    fallback_state["request"]["delivery_log_path"] = str(local_log_path)

    fallback_result = evidence_graph.invoke(fallback_state)

    assert fallback_result["email_mcp_fetch"]["status"] == "fallback"
    assert fallback_result["email_mcp_fetch"]["fallback_used"] is True
    assert fallback_result["deliveries"][0]["evidence_source"] == "local_log"
    assert any(
        error["category"] == "mcp"
        and error["node_name"] == "load_email_mcp_evidence"
        for error in fallback_result["errors"]
    )
