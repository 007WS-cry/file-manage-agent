from __future__ import annotations

from app.services.evidence_matching import (
    match_delivery_log_entries,
    match_pdf_to_source_version,
)
from app.state.models import (
    DeliveryLogEntry,
    DocumentRecord,
    FileRecord,
    PdfMatchJob,
    VersionGroupRecord,
)

"""本文件单元测试 PDF 来源和本地发送记录的确定性纯匹配规则。"""


def make_file(
    file_id: str,
    file_name: str,
    extension: str,
    *,
    sha256: str,
    modified_at: str,
    duplicate_of: str | None = None,
) -> FileRecord:
    """构造证据匹配测试使用的文件记录。

    Args:
        file_id: 测试文件唯一 ID。
        file_name: 包含扩展名的测试文件名。
        extension: 小写文件扩展名。
        sha256: 测试文件 SHA-256。
        modified_at: 带时区的 ISO 8601 修改时间。
        duplicate_of: 可选规范重复文件 ID。

    Returns:
        标记为已解析的 ``FileRecord``。
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
        duplicate_of=duplicate_of,
        parse_status="parsed",
        parse_error=None,
    )


def make_document(
    file_id: str,
    *,
    digest: str,
    preview: str,
) -> DocumentRecord:
    """构造证据匹配测试使用的标准化文档记录。

    Args:
        file_id: 文档对应的测试文件 ID。
        digest: 标准化内容 SHA-256。
        preview: 用于纯匹配的受限内容预览。

    Returns:
        不依赖真实产物文件的 ``DocumentRecord``。
    """
    return DocumentRecord(
        id=f"document:{file_id}",
        file_id=file_id,
        parser_name="unit-test/1.0",
        content_ref=f"/artifacts/{file_id}.json",
        content_preview=preview,
        normalized_digest=digest,
        structure_summary={},
        key_fields={"document_codes": ["HT-2026-001"]},
        warnings=[],
    )


def make_group(*file_ids: str) -> VersionGroupRecord:
    """构造包含指定文件的测试版本组。

    Args:
        file_ids: 需要归入测试版本组的文件 ID。

    Returns:
        置信度为一的 ``VersionGroupRecord``。
    """
    return VersionGroupRecord(
        id="group-contract",
        label="合同",
        file_ids=list(file_ids),
        grouping_signals=["单元测试固定分组"],
        confidence=1.0,
    )


def make_delivery_entry(
    *,
    attachment_name: str,
    attachment_sha256: str | None,
    normalized_digest: str | None = None,
) -> DeliveryLogEntry:
    """构造发送证据匹配测试使用的日志记录。

    Args:
        attachment_name: 日志中的附件名称。
        attachment_sha256: 可选附件原始哈希。
        normalized_digest: 可选附件标准化内容摘要。

    Returns:
        已满足固定协议的 ``DeliveryLogEntry``。
    """
    return DeliveryLogEntry(
        id="delivery-001",
        attachment_name=attachment_name,
        attachment_sha256=attachment_sha256,
        normalized_digest=normalized_digest,
        sent_at="2026-07-18T09:30:00+08:00",
        recipient_label="客户甲",
        customer_confirmed=True,
        evidence_ref="local-log://delivery-001",
    )


def test_pdf_exact_normalized_digest_selects_editable_source() -> None:
    """标准化摘要一致且没有并列时应选择对应可编辑来源。"""
    source = make_file(
        "source",
        "合同_最终版.docx",
        ".docx",
        sha256="1" * 64,
        modified_at="2026-07-18T08:00:00+08:00",
    )
    pdf = make_file(
        "pdf",
        "合同_最终版.pdf",
        ".pdf",
        sha256="2" * 64,
        modified_at="2026-07-18T08:05:00+08:00",
    )
    digest = "a" * 64
    job = PdfMatchJob(
        id="pdf-job",
        group_id="group-contract",
        pdf_file_id="pdf",
        source_candidate_ids=["source"],
        status="pending",
    )

    result = match_pdf_to_source_version(
        job,
        [source, pdf],
        [
            make_document("source", digest=digest, preview="合同编号 HT-2026-001 金额 1000"),
            make_document("pdf", digest=digest, preview="合同编号 HT-2026-001 金额 1000"),
        ],
    )

    assert result["source_file_id"] == "source"
    assert result["match_score"] >= 0.98
    assert "标准化内容摘要一致" in result["matched_signals"]


def test_pdf_near_tied_candidates_remain_unmatched() -> None:
    """两个来源候选近似并列时不得依靠稳定排序猜测来源。"""
    first = make_file(
        "source-a",
        "合同_v1.docx",
        ".docx",
        sha256="1" * 64,
        modified_at="2026-07-18T08:00:00+08:00",
    )
    second = make_file(
        "source-b",
        "合同_v2.docx",
        ".docx",
        sha256="2" * 64,
        modified_at="2026-07-18T08:00:00+08:00",
    )
    pdf = make_file(
        "pdf",
        "合同.pdf",
        ".pdf",
        sha256="3" * 64,
        modified_at="2026-07-18T09:00:00+08:00",
    )
    digest = "a" * 64
    job = PdfMatchJob(
        id="pdf-job",
        group_id="group-contract",
        pdf_file_id="pdf",
        source_candidate_ids=["source-a", "source-b"],
        status="pending",
    )

    result = match_pdf_to_source_version(
        job,
        [first, second, pdf],
        [
            make_document("source-a", digest=digest, preview="相同合同正文"),
            make_document("source-b", digest=digest, preview="相同合同正文"),
            make_document("pdf", digest=digest, preview="相同合同正文"),
        ],
    )

    assert result["source_file_id"] is None
    assert any("前两名候选分差" in signal for signal in result["matched_signals"])


def test_delivery_sha256_match_has_priority() -> None:
    """发送记录包含精确原始哈希时应返回最高置信度匹配。"""
    source = make_file(
        "source",
        "合同_最终版.docx",
        ".docx",
        sha256="1" * 64,
        modified_at="2026-07-18T08:00:00+08:00",
    )
    entry = make_delivery_entry(
        attachment_name="被重命名的附件.docx",
        attachment_sha256="1" * 64,
    )

    results = match_delivery_log_entries(
        [entry],
        [source],
        [make_document("source", digest="a" * 64, preview="合同正文")],
        [make_group("source")],
    )

    assert results[0]["file_id"] == "source"
    assert results[0]["match_method"] == "sha256"
    assert results[0]["confidence"] == 1.0


def test_delivery_normalized_digest_matches_when_hash_is_absent() -> None:
    """旧日志没有原始哈希时可使用唯一标准化内容摘要匹配。"""
    source = make_file(
        "source",
        "合同_最终版.docx",
        ".docx",
        sha256="1" * 64,
        modified_at="2026-07-18T08:00:00+08:00",
    )
    entry = make_delivery_entry(
        attachment_name="未知名称.docx",
        attachment_sha256=None,
        normalized_digest="a" * 64,
    )

    results = match_delivery_log_entries(
        [entry],
        [source],
        [make_document("source", digest="a" * 64, preview="合同正文")],
        [make_group("source")],
    )

    assert results[0]["file_id"] == "source"
    assert results[0]["match_method"] == "normalized_digest"
    assert results[0]["confidence"] == 0.95


def test_ambiguous_delivery_filename_remains_unmatched() -> None:
    """相同文件名对应多个非重复文件时不得选择任意一个版本。"""
    first = make_file(
        "source-a",
        "合同.docx",
        ".docx",
        sha256="1" * 64,
        modified_at="2026-07-18T08:00:00+08:00",
    )
    second = make_file(
        "source-b",
        "合同.docx",
        ".docx",
        sha256="2" * 64,
        modified_at="2026-07-18T09:00:00+08:00",
    )
    entry = make_delivery_entry(
        attachment_name="合同.docx",
        attachment_sha256=None,
    )

    results = match_delivery_log_entries(
        [entry],
        [first, second],
        [],
        [make_group("source-a", "source-b")],
    )

    assert results[0]["file_id"] is None
    assert results[0]["match_method"] == "unmatched"
    assert results[0]["confidence"] == 0.0
