from __future__ import annotations

import pytest

from app.services.recommendation import (
    apply_delivery_rules,
    apply_human_selection,
    apply_pdf_source_rules,
    create_scored_decision,
    find_editable_leaf_versions,
    recommend_main_version,
    score_version_candidates,
)
from app.state.models import FileRecord

"""本文件单元测试可解释候选评分、自动推荐和人工确认约束。"""


def make_file_record(
    file_id: str,
    file_name: str,
    modified_at: str,
) -> FileRecord:
    """构造推荐规则测试使用的文件记录。

    Args:
        file_id: 测试使用的稳定文件 ID。
        file_name: 包含版本弱标记的文件名。
        modified_at: 带时区的 ISO 8601 修改时间。

    Returns:
        可参与候选评分的已解析 DOCX 文件记录。
    """
    return FileRecord(
        id=file_id,
        absolute_path=f"/readonly/{file_name}",
        file_name=file_name,
        normalized_stem="合同",
        extension=".docx",
        size_bytes=1,
        modified_at=modified_at,
        sha256=file_id * 16,
        duplicate_of=None,
        parse_status="parsed",
        parse_error=None,
    )


def test_latest_editable_final_leaf_is_auto_selected() -> None:
    """完整线性链中最新、可编辑且带最终标记的叶子应自动成为主版本。"""
    files = [
        make_file_record("v1", "合同_v1.docx", "2026-01-01T00:00:00+00:00"),
        make_file_record("final", "合同_最终版.docx", "2026-01-02T00:00:00+00:00"),
    ]
    group = {
        "id": "group",
        "label": "合同",
        "file_ids": ["v1", "final"],
        "grouping_signals": [],
        "confidence": 0.95,
    }
    chain = {
        "id": "chain",
        "group_id": "group",
        "ordered_file_ids": ["v1", "final"],
        "leaf_file_ids": ["final"],
        "is_complete": True,
        "warnings": [],
    }

    scores, reasons = score_version_candidates(group, files, chain)
    decision = recommend_main_version(
        group,
        files,
        chain,
        [],
        auto_select_threshold=0.82,
    )

    assert scores["final"] > scores["v1"]
    assert any("叶子节点" in reason for reason in reasons["final"])
    assert decision["recommended_file_id"] == "final"
    assert decision["selected_by"] == "rule"
    assert decision["needs_human_review"] is False


def test_branch_forces_human_review() -> None:
    """检测到版本分叉时，即使候选分数较高也必须人工确认。"""
    files = [
        make_file_record("root", "合同_v1.docx", "2026-01-01T00:00:00+00:00"),
        make_file_record("left", "合同_v2.docx", "2026-01-02T00:00:00+00:00"),
        make_file_record("right", "合同_最终版.docx", "2026-01-03T00:00:00+00:00"),
    ]
    group = {
        "id": "group",
        "label": "合同",
        "file_ids": ["root", "left", "right"],
        "grouping_signals": [],
        "confidence": 0.9,
    }
    chain = {
        "id": "chain",
        "group_id": "group",
        "ordered_file_ids": ["root", "left", "right"],
        "leaf_file_ids": ["left", "right"],
        "is_complete": True,
        "warnings": [],
    }
    branch = {
        "id": "branch",
        "group_id": "group",
        "root_file_id": "root",
        "child_file_ids": ["left", "right"],
        "reason": "测试分叉",
        "confidence": 0.9,
    }

    decision = recommend_main_version(group, files, chain, [branch])

    assert decision["needs_human_review"] is True
    assert decision["selected_by"] == "unresolved"
    assert any("版本分叉" in reason for reason in decision["reasons"])


def test_human_selection_accepts_only_group_member() -> None:
    """人工选择只能引用当前组成员，并且必须保留完整版本列表。"""
    group = {
        "id": "group",
        "label": "合同",
        "file_ids": ["a", "b"],
        "grouping_signals": [],
        "confidence": 0.8,
    }
    decision = {
        "id": "decision:group",
        "group_id": "group",
        "candidate_scores": {"a": 0.5, "b": 0.5},
        "recommended_file_id": "a",
        "reasons": ["并列"],
        "confidence": 0.4,
        "needs_human_review": True,
        "selected_by": "unresolved",
        "preserve_file_ids": ["a", "b"],
    }

    updated = apply_human_selection(decision, group, "b")

    assert updated["recommended_file_id"] == "b"
    assert updated["selected_by"] == "human"
    assert updated["preserve_file_ids"] == ["a", "b"]

    with pytest.raises(ValueError, match="不属于当前版本组"):
        apply_human_selection(decision, group, "outside")


def test_candidate_set_excludes_duplicate_and_marks_editable_leaf() -> None:
    """候选集合应排除完全重复件，并单独标记可编辑叶子文件。"""
    files = [
        make_file_record("root", "合同_v1.docx", "2026-01-01T00:00:00+00:00"),
        make_file_record("leaf", "合同_v2.docx", "2026-01-02T00:00:00+00:00"),
        make_file_record("copy", "合同_v2_副本.docx", "2026-01-02T00:00:00+00:00"),
    ]
    files[2]["duplicate_of"] = "leaf"
    group = {
        "id": "group",
        "label": "合同",
        "file_ids": ["root", "leaf", "copy"],
        "grouping_signals": [],
        "confidence": 0.9,
    }
    chain = {
        "id": "chain",
        "group_id": "group",
        "ordered_file_ids": ["root", "leaf"],
        "leaf_file_ids": ["leaf"],
        "is_complete": True,
        "warnings": [],
    }

    candidate_set = find_editable_leaf_versions(group, files, chain)

    assert candidate_set["candidate_file_ids"] == ["root", "leaf"]
    assert candidate_set["editable_leaf_file_ids"] == ["leaf"]


def test_delivery_and_pdf_source_evidence_adjust_scores_transparently() -> None:
    """发送确认和 PDF 来源证据应提高源版本评分并保留解释。"""
    files = [
        make_file_record("source", "合同_最终版.docx", "2026-01-02T00:00:00+00:00"),
        FileRecord(
            id="pdf",
            absolute_path="/readonly/合同.pdf",
            file_name="合同.pdf",
            normalized_stem="合同",
            extension=".pdf",
            size_bytes=1,
            modified_at="2026-01-03T00:00:00+00:00",
            sha256="p" * 64,
            duplicate_of=None,
            parse_status="parsed",
            parse_error=None,
        ),
    ]
    group = {
        "id": "group",
        "label": "合同",
        "file_ids": ["source", "pdf"],
        "grouping_signals": [],
        "confidence": 0.9,
    }
    chain = {
        "id": "chain",
        "group_id": "group",
        "ordered_file_ids": ["source", "pdf"],
        "leaf_file_ids": ["source", "pdf"],
        "is_complete": True,
        "warnings": [],
    }
    candidate_set = find_editable_leaf_versions(group, files, chain)
    decision = create_scored_decision(group, files, chain, candidate_set)
    source_before = decision["candidate_scores"]["source"]
    pdf_before = decision["candidate_scores"]["pdf"]

    decision = apply_delivery_rules(
        decision,
        [
            {
                "id": "delivery",
                "group_id": "group",
                "file_id": "source",
                "evidence_source": "local_log",
                "sent_at": "2026-01-04T00:00:00+00:00",
                "recipient_label": "客户A",
                "evidence_ref": "delivery:1",
                "match_method": "sha256",
                "customer_confirmed": True,
                "confidence": 1.0,
            }
        ],
    )
    decision = apply_pdf_source_rules(
        decision,
        [
            {
                "id": "pdf-export",
                "group_id": "group",
                "pdf_file_id": "pdf",
                "source_file_id": "source",
                "match_score": 1.0,
                "matched_signals": ["标准化内容一致"],
                "confidence": 1.0,
            }
        ],
    )

    assert decision["candidate_scores"]["source"] > source_before
    assert decision["candidate_scores"]["pdf"] < pdf_before
    assert any("客户已确认" in reason for reason in decision["reasons"])
    assert any("PDF 来源证据" in reason for reason in decision["reasons"])
