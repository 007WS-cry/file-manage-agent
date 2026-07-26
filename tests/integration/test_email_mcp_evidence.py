from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from app.graphs.evidence import evidence_graph
from app.mcp_servers.mock_email import create_mock_email_mcp_server
from app.services.recommendation import apply_delivery_rules
from app.state.models import (
    DecisionRecord,
    DocumentRecord,
    EvidenceGraphState,
    FileRecord,
    VersionGroupRecord,
)

"""本文件集成测试真实 Streamable HTTP 模拟 MCP 到 DeliveryRecord 与推荐加权的链路。"""


def reserve_local_port() -> int:
    """向操作系统申请一个仅供当前测试服务使用的本地端口。

    Returns:
        释放后可立即交给 Uvicorn 监听的本地端口。
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    return port


@contextmanager
def run_mock_email_mcp(data_path: Path) -> Iterator[str]:
    """在后台线程运行真实 MCP ASGI lifespan，并在测试后安全关闭。

    Args:
        data_path: 当前测试的脱敏模拟邮件 JSON 文件。

    Yields:
        客户端可访问的 Streamable HTTP MCP URL。
    """
    port = reserve_local_port()
    mcp_server = create_mock_email_mcp_server(
        data_path,
        host="127.0.0.1",
        port=port,
        log_level="ERROR",
    )
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            mcp_server.streamable_http_app(),
            host="127.0.0.1",
            port=port,
            log_level="error",
            log_config=None,
        )
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uvicorn_server.started:
            break
        time.sleep(0.02)
    if not uvicorn_server.started:
        uvicorn_server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("测试模拟邮件 MCP 未能启动")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=5)


def make_evidence_state(server_url: str) -> EvidenceGraphState:
    """构造只包含一个可匹配 DOCX 的邮件 MCP Evidence 状态。

    Args:
        server_url: 当前测试模拟服务的 MCP URL。

    Returns:
        可直接提交给独立 Evidence 子图的状态。
    """
    file_record = FileRecord(
        id="file-contract-v3",
        absolute_path="/readonly/contract-v3.docx",
        file_name="contract-v3.docx",
        normalized_stem="contract",
        extension=".docx",
        size_bytes=100,
        modified_at="2026-07-18T08:00:00+08:00",
        sha256="a" * 64,
        duplicate_of=None,
        parse_status="parsed",
        parse_error=None,
    )
    document = DocumentRecord(
        id="document-contract-v3",
        file_id=file_record["id"],
        parser_name="integration-test/1.0",
        content_ref="/artifacts/contract-v3.json",
        content_preview="contract",
        normalized_digest="b" * 64,
        structure_summary={},
        key_fields={},
        warnings=[],
    )
    group = VersionGroupRecord(
        id="group-contract",
        label="contract",
        file_ids=[file_record["id"]],
        grouping_signals=["固定测试分组"],
        confidence=1.0,
    )
    return EvidenceGraphState(
        request={
            "root_directory": "/readonly",
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": False,
        },
        email_mcp={
            "enabled": True,
            "server_url": server_url,
            "timeout_seconds": 3.0,
            "max_results": 10,
        },
        email_mcp_fetch={
            "status": "pending",
            "record_count": 0,
            "fallback_used": False,
            "error_summary": None,
        },
        email_mcp_entries=[],
        files=[file_record],
        documents=[document],
        version_groups=[group],
        pdf_candidate_ids=[],
        pdf_match_jobs=[],
        delivery_log_entries=[],
        pdf_exports=[],
        deliveries=[],
        errors=[],
    )


def test_email_mcp_evidence_enters_delivery_and_affects_recommendation(
    tmp_path: Path,
) -> None:
    """真实 MCP 证据应匹配文件，并按客户确认规则提高候选评分。"""
    data_path = tmp_path / "mock_email.json"
    data_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "records": [
                    {
                        "id": "email-evidence-001",
                        "attachment_name": "contract-v3.docx",
                        "attachment_sha256": "a" * 64,
                        "normalized_digest": None,
                        "sent_at": "2026-07-18T09:30:00+08:00",
                        "recipient_label": "customer-A",
                        "customer_confirmed": True,
                        "evidence_ref": "email-mcp://mock/thread-001/attachment-001",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with run_mock_email_mcp(data_path) as server_url:
        result = evidence_graph.invoke(make_evidence_state(server_url))

    delivery = result["deliveries"][0]
    assert result["email_mcp_fetch"]["status"] == "available"
    assert delivery["evidence_source"] == "email_mcp"
    assert delivery["file_id"] == "file-contract-v3"
    base_decision = DecisionRecord(
        id="decision-contract",
        group_id="group-contract",
        recommended_file_id="file-contract-v3",
        candidate_scores={"file-contract-v3": 0.60},
        reasons=[],
        confidence=0.60,
        needs_human_review=True,
        selected_by="unresolved",
    )
    weighted = apply_delivery_rules(base_decision, [delivery])
    assert weighted["candidate_scores"]["file-contract-v3"] == 0.78
    assert any("客户已确认" in reason for reason in weighted["reasons"])
