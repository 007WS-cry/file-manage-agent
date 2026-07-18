from __future__ import annotations

import hashlib
from pathlib import Path

from app.services.content_normalizer import normalize_document_content
from app.services.document_grouping import normalize_filename_stem
from app.services.version_graph import (
    build_version_chains,
    build_version_edges,
    compare_document_pair,
    detect_version_branches,
    generate_candidate_pairs,
)
from app.state.models import FileRecord, RawExtractedContent, VersionEdge

"""本文件单元测试候选对、差异、版本边、分叉和版本链构建规则。"""


def make_file_record(
    path: Path,
    file_id: str,
    modified_at: str,
    *,
    duplicate_of: str | None = None,
) -> FileRecord:
    """构造版本图测试使用的文件记录。

    Args:
        path: 测试文件路径。
        file_id: 测试使用的稳定文件 ID。
        modified_at: 带时区的 ISO 8601 修改时间。
        duplicate_of: 可选完全重复规范文件 ID。

    Returns:
        可参与版本图计算的 ``FileRecord``。
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
        duplicate_of=duplicate_of,
        parse_status="duplicate" if duplicate_of else "parsed",
        parse_error=None,
    )


def make_raw_content(text: str) -> RawExtractedContent:
    """构造版本差异测试使用的标准解析器输出。

    Args:
        text: 等待标准化的测试文本。

    Returns:
        包含最小 DOCX 结构的解析结果。
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


def test_build_linear_version_chain_from_document_diff(tmp_path: Path) -> None:
    """两个有明确版本号的相似文档应形成有向线性版本链。"""
    input_root = tmp_path / "input"
    artifact_root = tmp_path / "artifacts"
    input_root.mkdir()
    left_path = input_root / "合同_v1.docx"
    right_path = input_root / "合同_v2.docx"
    left_text = "合同编号 HT-2026-001 金额 CNY 1000 条款 A"
    right_text = "合同编号 HT-2026-001 金额 CNY 1200 条款 A"
    left_path.write_text(left_text, encoding="utf-8")
    right_path.write_text(right_text, encoding="utf-8")
    left_file = make_file_record(left_path, "v1", "2026-01-01T00:00:00+00:00")
    right_file = make_file_record(right_path, "v2", "2026-01-02T00:00:00+00:00")
    left_document = normalize_document_content(
        left_file,
        make_raw_content(left_text),
        artifact_root,
        input_root=input_root,
    )
    right_document = normalize_document_content(
        right_file,
        make_raw_content(right_text),
        artifact_root,
        input_root=input_root,
    )
    group = {
        "id": "group",
        "label": "合同",
        "file_ids": ["v1", "v2"],
        "grouping_signals": ["测试分组"],
        "confidence": 0.9,
    }

    jobs = generate_candidate_pairs([group], [left_file, right_file])
    diff = compare_document_pair(
        "group",
        left_file,
        right_file,
        left_document,
        right_document,
    )
    edges = build_version_edges([group], [left_file, right_file], [diff])
    chains = build_version_chains([group], [left_file, right_file], edges)

    assert len(jobs) == 1
    assert diff["older_file_id"] == "v1"
    assert diff["newer_file_id"] == "v2"
    assert diff["key_changes"]
    assert [(item["parent_file_id"], item["child_file_id"]) for item in edges] == [("v1", "v2")]
    assert chains[0]["ordered_file_ids"] == ["v1", "v2"]
    assert chains[0]["is_complete"] is True


def test_build_duplicate_edge_without_content_comparison(tmp_path: Path) -> None:
    """SHA-256 重复件应直接形成 duplicate_of 边且不生成候选比较对。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    canonical_path = input_root / "合同_v1.docx"
    duplicate_path = input_root / "合同_v1_副本.docx"
    canonical_path.write_text("相同正文", encoding="utf-8")
    duplicate_path.write_text("相同正文", encoding="utf-8")
    canonical = make_file_record(canonical_path, "canonical", "2026-01-01T00:00:00+00:00")
    duplicate = make_file_record(
        duplicate_path,
        "duplicate",
        "2026-01-02T00:00:00+00:00",
        duplicate_of="canonical",
    )
    group = {
        "id": "group",
        "label": "合同",
        "file_ids": ["canonical", "duplicate"],
        "grouping_signals": ["SHA-256 完全一致"],
        "confidence": 1.0,
    }

    jobs = generate_candidate_pairs([group], [canonical, duplicate])
    edges = build_version_edges([group], [canonical, duplicate], [])

    assert jobs == []
    assert len(edges) == 1
    assert edges[0]["relation"] == "duplicate_of"
    assert edges[0]["parent_file_id"] == "canonical"


def test_detect_version_branch_from_shared_parent() -> None:
    """同一父版本存在两个直接派生版本时应产生分叉记录。"""
    group = {
        "id": "group",
        "label": "合同",
        "file_ids": ["root", "left", "right"],
        "grouping_signals": [],
        "confidence": 0.9,
    }
    edges = [
        VersionEdge(
            id="edge-left",
            group_id="group",
            parent_file_id="root",
            child_file_id="left",
            relation="derived_from",
            evidence=["测试"],
            confidence=0.9,
        ),
        VersionEdge(
            id="edge-right",
            group_id="group",
            parent_file_id="root",
            child_file_id="right",
            relation="derived_from",
            evidence=["测试"],
            confidence=0.8,
        ),
    ]

    branches = detect_version_branches([group], edges)

    assert len(branches) == 1
    assert branches[0]["root_file_id"] == "root"
    assert branches[0]["child_file_ids"] == ["left", "right"]
