from __future__ import annotations

import json
from pathlib import Path

from app.graphs.evidence import evidence_graph
from app.graphs.file_governance import file_governance_graph
from app.nodes.subgraphs_nodes import run_evidence_subgraph
from app.state.factories import create_initial_state
from app.state.models import (
    DocumentRecord,
    EvidenceGraphState,
    FileRecord,
    VersionGroupRecord,
)

"""本文件集成测试独立 Evidence 子图的分支、Send 并行汇合和状态隔离。"""


def make_file(
    file_id: str,
    file_name: str,
    extension: str,
    *,
    sha256: str,
    modified_at: str,
) -> FileRecord:
    """构造 Evidence 子图集成测试使用的已解析文件记录。

    Args:
        file_id: 测试文件唯一 ID。
        file_name: 包含扩展名的测试文件名。
        extension: 小写文件扩展名。
        sha256: 测试文件原始 SHA-256。
        modified_at: 带时区的 ISO 8601 修改时间。

    Returns:
        不依赖真实业务文件的 ``FileRecord``。
    """
    return FileRecord(
        id=file_id,
        absolute_path=f"/readonly/{file_name}",
        file_name=file_name,
        normalized_stem="合同",
        extension=extension,
        size_bytes=100,
        modified_at=modified_at,
        sha256=sha256,
        duplicate_of=None,
        parse_status="parsed",
        parse_error=None,
    )


def make_document(file_id: str, *, digest: str) -> DocumentRecord:
    """构造具有相同合同正文的标准化文档状态。

    Args:
        file_id: 文档对应的测试文件 ID。
        digest: 标准化内容 SHA-256。

    Returns:
        仅使用预览和关键字段参与纯匹配的 ``DocumentRecord``。
    """
    return DocumentRecord(
        id=f"document:{file_id}",
        file_id=file_id,
        parser_name="integration-test/1.0",
        content_ref=f"/artifacts/{file_id}.json",
        content_preview="合同编号 HT-2026-001 金额 CNY 1000 条款 A",
        normalized_digest=digest,
        structure_summary={},
        key_fields={"document_codes": ["HT-2026-001"], "amounts": ["CNY 1000"]},
        warnings=[],
    )


def make_group(*file_ids: str) -> VersionGroupRecord:
    """构造包含指定文件的单一合同版本组。

    Args:
        file_ids: 应归入测试版本组的文件 ID。

    Returns:
        置信度为一的 ``VersionGroupRecord``。
    """
    return VersionGroupRecord(
        id="group-contract",
        label="合同",
        file_ids=list(file_ids),
        grouping_signals=["集成测试固定分组"],
        confidence=1.0,
    )


def write_delivery_log(path: Path, *, source_sha256: str) -> bytes:
    """写入 Evidence 集成测试使用的固定协议发送日志。

    Args:
        path: 测试发送日志文件路径。
        source_sha256: 应精确匹配可编辑源版本的 SHA-256。

    Returns:
        写入后的日志字节快照，用于验证图运行保持日志只读。
    """
    payload = {
        "schema_version": "1.0",
        "deliveries": [
            {
                "id": "delivery-001",
                "attachment_name": "合同_最终版.docx",
                "attachment_sha256": source_sha256,
                "normalized_digest": None,
                "sent_at": "2026-07-18T09:30:00+08:00",
                "recipient_label": "客户甲",
                "customer_confirmed": True,
                "evidence_ref": "local-log://delivery-001",
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path.read_bytes()


def make_evidence_state(
    files: list[FileRecord],
    documents: list[DocumentRecord],
    group: VersionGroupRecord,
    *,
    delivery_log_path: str | None,
) -> EvidenceGraphState:
    """构造可直接提交给独立 Evidence 子图的完整状态。

    Args:
        files: 子图使用的文件记录。
        documents: 子图使用的标准化文档记录。
        group: 文件所属的唯一版本组。
        delivery_log_path: 可选本地发送记录路径。

    Returns:
        所有私有队列和 reducer 列表均已初始化的 ``EvidenceGraphState``。
    """
    return EvidenceGraphState(
        request={
            "root_directory": "/readonly",
            "recursive": True,
            "allowed_extensions": [".docx", ".pdf"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": delivery_log_path,
            "use_llm_summary": False,
        },
        files=files,
        documents=documents,
        version_groups=[group],
        pdf_candidate_ids=[],
        pdf_match_jobs=[],
        delivery_log_entries=[],
        pdf_exports=[],
        deliveries=[],
        errors=[],
    )


def test_evidence_graph_fans_out_multiple_pdfs_and_joins_results(
    tmp_path: Path,
) -> None:
    """两个 PDF 应并行匹配后统一汇合，并继续匹配本地发送记录。"""
    source_sha256 = "1" * 64
    source = make_file(
        "source",
        "合同_最终版.docx",
        ".docx",
        sha256=source_sha256,
        modified_at="2026-07-18T08:00:00+08:00",
    )
    first_pdf = make_file(
        "pdf-a",
        "合同_交付版A.pdf",
        ".pdf",
        sha256="2" * 64,
        modified_at="2026-07-18T08:10:00+08:00",
    )
    second_pdf = make_file(
        "pdf-b",
        "合同_交付版B.pdf",
        ".pdf",
        sha256="3" * 64,
        modified_at="2026-07-18T08:20:00+08:00",
    )
    digest = "a" * 64
    log_path = tmp_path / "delivery_log.json"
    original_log = write_delivery_log(log_path, source_sha256=source_sha256)
    state = make_evidence_state(
        [source, first_pdf, second_pdf],
        [
            make_document("source", digest=digest),
            make_document("pdf-a", digest=digest),
            make_document("pdf-b", digest=digest),
        ],
        make_group("source", "pdf-a", "pdf-b"),
        delivery_log_path=str(log_path),
    )

    result = evidence_graph.invoke(state)

    assert len(result["pdf_match_jobs"]) == 2
    assert {item["status"] for item in result["pdf_match_jobs"]} == {"completed"}
    assert {item["pdf_file_id"] for item in result["pdf_exports"]} == {
        "pdf-a",
        "pdf-b",
    }
    assert {item["source_file_id"] for item in result["pdf_exports"]} == {"source"}
    assert result["deliveries"][0]["file_id"] == "source"
    assert result["deliveries"][0]["match_method"] == "sha256"
    assert result["errors"] == []
    assert log_path.read_bytes() == original_log


def test_evidence_graph_skips_pdf_and_delivery_branches_when_empty() -> None:
    """没有 PDF 且未配置发送日志时仍应从 START 正常执行到 END。"""
    source = make_file(
        "source",
        "合同.docx",
        ".docx",
        sha256="1" * 64,
        modified_at="2026-07-18T08:00:00+08:00",
    )
    state = make_evidence_state(
        [source],
        [make_document("source", digest="a" * 64)],
        make_group("source"),
        delivery_log_path=None,
    )

    result = evidence_graph.invoke(state)

    assert result["pdf_candidate_ids"] == []
    assert result["pdf_match_jobs"] == []
    assert result["pdf_exports"] == []
    assert result["delivery_log_entries"] == []
    assert result["deliveries"] == []
    assert result["errors"] == []


def test_evidence_graph_degrades_when_delivery_log_is_invalid(tmp_path: Path) -> None:
    """本地日志解析失败应记录非致命错误并保留其他证据处理能力。"""
    source = make_file(
        "source",
        "合同.docx",
        ".docx",
        sha256="1" * 64,
        modified_at="2026-07-18T08:00:00+08:00",
    )
    invalid_log_path = tmp_path / "delivery_log.json"
    invalid_log_path.write_text("{invalid", encoding="utf-8")
    state = make_evidence_state(
        [source],
        [make_document("source", digest="a" * 64)],
        make_group("source"),
        delivery_log_path=str(invalid_log_path),
    )

    result = evidence_graph.invoke(state)

    assert result["deliveries"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["node_name"] == "load_local_delivery_log"
    assert result["errors"][0]["fatal"] is False


def test_evidence_wrapper_filters_private_subgraph_state(tmp_path: Path) -> None:
    """独立包装节点只能返回顶层允许保存的证据和错误字段。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    source_sha256 = "1" * 64
    source = make_file(
        "source",
        "合同_最终版.docx",
        ".docx",
        sha256=source_sha256,
        modified_at="2026-07-18T08:00:00+08:00",
    )
    pdf = make_file(
        "pdf",
        "合同.pdf",
        ".pdf",
        sha256="2" * 64,
        modified_at="2026-07-18T08:10:00+08:00",
    )
    digest = "a" * 64
    log_path = tmp_path / "delivery_log.json"
    write_delivery_log(log_path, source_sha256=source_sha256)
    top_state = create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx", ".pdf"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": str(log_path),
            "use_llm_summary": False,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(tmp_path / "artifacts"),
            "report_root": str(tmp_path / "reports"),
        },
    )
    top_state["files"] = [source, pdf]
    top_state["documents"] = [
        make_document("source", digest=digest),
        make_document("pdf", digest=digest),
    ]
    top_state["version_groups"] = [make_group("source", "pdf")]

    update = run_evidence_subgraph(top_state)

    assert set(update) == {"pdf_exports", "deliveries", "errors"}
    assert update["pdf_exports"][0]["source_file_id"] == "source"
    assert update["deliveries"][0]["file_id"] == "source"


def test_evidence_subgraph_remains_independent_after_top_graph_registration() -> None:
    """第四批接入顶层图后，Evidence 仍应保留可独立调用的完整节点结构。"""
    top_node_ids = set(file_governance_graph.get_graph().nodes)
    evidence_node_ids = set(evidence_graph.get_graph().nodes)

    assert "run_evidence_subgraph" in top_node_ids
    assert "collect_pdf_candidates" in evidence_node_ids
    assert "validate_evidence_confidence" in evidence_node_ids
