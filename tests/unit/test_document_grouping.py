from __future__ import annotations

import hashlib
from pathlib import Path

from app.services.content_normalizer import normalize_document_content
from app.services.document_grouping import group_related_documents, normalize_filename_stem
from app.state.models import FileRecord, RawExtractedContent

"""本文件单元测试文件名归一化和基于标准化内容的文档版本分组。"""


def make_file_record(
    path: Path,
    file_id: str,
    modified_at: str,
) -> FileRecord:
    """构造文档分组测试使用的确定性文件记录。

    Args:
        path: 测试文件路径。
        file_id: 测试使用的稳定文件 ID。
        modified_at: 带时区的 ISO 8601 修改时间。

    Returns:
        标记为已解析的 ``FileRecord``。
    """
    content = path.read_bytes()
    return FileRecord(
        id=file_id,
        absolute_path=str(path),
        file_name=path.name,
        normalized_stem=normalize_filename_stem(path.name),
        extension=path.suffix.lower(),
        size_bytes=len(content),
        modified_at=modified_at,
        sha256=hashlib.sha256(content).hexdigest(),
        duplicate_of=None,
        parse_status="parsed",
        parse_error=None,
    )


def make_raw_content(text: str) -> RawExtractedContent:
    """构造文档分组测试使用的统一解析结果。

    Args:
        text: 等待标准化的测试文本。

    Returns:
        最小 DOCX 结构的 ``RawExtractedContent``。
    """
    return RawExtractedContent(
        text=text,
        structure={
            "document_type": "docx",
            "parser": "unit-test/1.0",
            "paragraph_count": 1,
            "table_count": 0,
            "truncated": False,
        },
        key_fields={},
        warnings=[],
    )


def test_normalize_filename_stem_removes_version_markers() -> None:
    """常见版本号和最终版标记应归一化为同一文件名主体。"""
    assert normalize_filename_stem("合同_v1.docx") == "合同"
    assert normalize_filename_stem("合同_最终版.docx") == "合同"
    assert normalize_filename_stem("合同_20260101.docx") == "合同"


def test_group_related_versions_and_store_normalized_artifacts(tmp_path: Path) -> None:
    """名称和内容接近的文档应合组，标准化内容只能写到隔离产物目录。"""
    input_root = tmp_path / "input"
    artifact_root = tmp_path / "artifacts"
    input_root.mkdir()
    specifications = (
        ("合同_v1.docx", "v1", "2026-01-01T00:00:00+00:00", "合同编号 HT-2026-001 金额 CNY 1000 条款 A"),
        ("合同_v2.docx", "v2", "2026-01-02T00:00:00+00:00", "合同编号 HT-2026-001 金额 CNY 1200 条款 A"),
    )
    files = []
    documents = []
    source_snapshots = {}
    for file_name, file_id, modified_at, text in specifications:
        path = input_root / file_name
        path.write_text(text, encoding="utf-8")
        source_snapshots[path] = path.read_bytes()
        file_record = make_file_record(path, file_id, modified_at)
        files.append(file_record)
        documents.append(
            normalize_document_content(
                file_record,
                make_raw_content(text),
                artifact_root,
                input_root=input_root,
            )
        )

    groups = group_related_documents(files, documents, similarity_threshold=0.72)

    assert len(groups) == 1
    assert set(groups[0]["file_ids"]) == {"v1", "v2"}
    assert all(Path(item["content_ref"]).parent == artifact_root / "normalized" for item in documents)
    assert all(path.read_bytes() == content for path, content in source_snapshots.items())
    assert not any(path.suffix == ".json" for path in input_root.rglob("*"))


def test_same_name_without_content_support_stays_separate(tmp_path: Path) -> None:
    """不同目录中的同名无关文档不能只凭文件名被合并。"""
    input_root = tmp_path / "input"
    artifact_root = tmp_path / "artifacts"
    left_directory = input_root / "客户甲"
    right_directory = input_root / "客户乙"
    left_directory.mkdir(parents=True)
    right_directory.mkdir(parents=True)
    left_path = left_directory / "报价单_v1.docx"
    right_path = right_directory / "报价单_v2.docx"
    left_path.write_text("苹果 香蕉 橙子", encoding="utf-8")
    right_path.write_text("服务器 数据库 网络", encoding="utf-8")
    left_file = make_file_record(left_path, "left", "2026-01-01T00:00:00+00:00")
    right_file = make_file_record(right_path, "right", "2026-01-02T00:00:00+00:00")
    documents = [
        normalize_document_content(
            left_file,
            make_raw_content("苹果 香蕉 橙子"),
            artifact_root,
            input_root=input_root,
        ),
        normalize_document_content(
            right_file,
            make_raw_content("服务器 数据库 网络"),
            artifact_root,
            input_root=input_root,
        ),
    ]

    groups = group_related_documents(
        [left_file, right_file],
        documents,
        similarity_threshold=0.72,
    )

    assert len(groups) == 2
