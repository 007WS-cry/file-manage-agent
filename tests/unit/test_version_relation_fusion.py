from __future__ import annotations

from app.services.version_relation_fusion import (
    fuse_version_relations,
    infer_deterministic_relation,
)
from app.state.models import (
    FileRecord,
    VersionRelationAssessmentRecord,
    VersionRelationConstraints,
    VersionRelationType,
)

"""本文件单元测试双轨版本关系的硬约束、共识增强与冲突审核。"""


def make_file(file_id: str, modified_at: str) -> FileRecord:
    """构造弱时间方向关系测试使用的 DOCX 文件记录。

    Args:
        file_id: 测试文件的稳定 ID 和哈希种子。
        modified_at: 带时区的 ISO 8601 修改时间。

    Returns:
        具有相同规范名称但不同哈希的文件记录。
    """
    return FileRecord(
        id=file_id,
        absolute_path=f"/readonly/合同_{file_id}.docx",
        file_name=f"合同_{file_id}.docx",
        normalized_stem="合同",
        extension=".docx",
        size_bytes=100,
        modified_at=modified_at,
        sha256=(file_id * 64)[:64],
        duplicate_of=None,
        parse_status="parsed",
        parse_error=None,
    )


def make_constraints(
    *,
    exact_hash_match: bool = False,
    parent_child_supported: bool = False,
    export_supported: bool = False,
    semantic_duplicate_supported: bool = False,
    parallel_branch_supported: bool = False,
    unrelated_supported: bool = False,
) -> VersionRelationConstraints:
    """构造融合规则测试使用的完整关系约束快照。

    Args:
        exact_hash_match: 是否存在不可覆盖的完全相同哈希。
        parent_child_supported: 是否允许直接修订父子边。
        export_supported: 是否允许可编辑源到 PDF 导出边。
        semantic_duplicate_supported: 是否允许语义重复候选。
        parallel_branch_supported: 是否允许并行分支候选。
        unrelated_supported: 是否允许无关文件候选。

    Returns:
        字段完整的关系约束快照。
    """
    return VersionRelationConstraints(
        exact_hash_match=exact_hash_match,
        parent_child_supported=parent_child_supported,
        export_supported=export_supported,
        semantic_duplicate_supported=semantic_duplicate_supported,
        parallel_branch_supported=parallel_branch_supported,
        unrelated_supported=unrelated_supported,
    )


def make_assessment(
    relation: VersionRelationType,
    *,
    confidence: float = 0.90,
) -> VersionRelationAssessmentRecord:
    """构造已经通过 Schema 和引用白名单校验的 LLM 关系候选。

    Args:
        relation: 封闭版本关系类型。
        confidence: LLM 候选置信度。

    Returns:
        可直接交给融合函数的关系候选记录。
    """
    return VersionRelationAssessmentRecord(
        relation=relation,
        reason="测试关系证据支持该候选。",
        evidence_refs=["diff:comparison:similarity"],
        confidence=confidence,
    )


def test_exact_hash_fact_cannot_be_overridden_by_llm() -> None:
    """哈希完全一致时必须固定为重复关系并忽略 LLM 冲突候选。"""
    result = fuse_version_relations(
        "semantic_duplicate",
        1.0,
        make_constraints(exact_hash_match=True, semantic_duplicate_supported=True),
        make_assessment("unrelated"),
    )

    assert result[:4] == (
        "semantic_duplicate",
        "deterministic_only",
        1.0,
        False,
    )


def test_consensus_boosts_relation_confidence() -> None:
    """LLM 与确定性关系一致时应提高最终关系置信度。"""
    result = fuse_version_relations(
        "direct_revision",
        0.78,
        make_constraints(parent_child_supported=True),
        make_assessment("direct_revision", confidence=0.92),
    )

    assert result[0] == "direct_revision"
    assert result[1] == "consensus"
    assert result[2] > 0.78
    assert result[3] is False


def test_conflicting_supported_relations_require_human_review() -> None:
    """两条轨道提出不同受支持关系时不得自动改图并必须人工审核。"""
    result = fuse_version_relations(
        "direct_revision",
        0.82,
        make_constraints(
            parent_child_supported=True,
            parallel_branch_supported=True,
        ),
        make_assessment("parallel_branch", confidence=0.87),
    )

    assert result[0] == "uncertain"
    assert result[1] == "conflict_review"
    assert result[3] is True


def test_unsupported_parent_child_candidate_is_rejected() -> None:
    """时间和内容均不支持父子关系时 LLM 不得建立直接修订边。"""
    result = fuse_version_relations(
        "uncertain",
        0.42,
        make_constraints(),
        make_assessment("direct_revision", confidence=0.94),
    )

    assert result[0] == "uncertain"
    assert result[1] == "constrained_rejection"
    assert result[3] is True


def test_supported_llm_candidate_can_resolve_deterministic_uncertainty() -> None:
    """确定性结果不明确时，高置信且受约束的 LLM 候选可以影响版本图。"""
    deterministic_relation, deterministic_confidence, constraints, _ = (
        infer_deterministic_relation(
            make_file("a", "2026-01-01T00:00:00+00:00"),
            make_file("b", "2026-01-02T00:00:00+00:00"),
            content_similarity=0.70,
            structural_similarity=0.80,
            older_file_id="a",
            newer_file_id="b",
            ordering_confidence=0.60,
        )
    )
    result = fuse_version_relations(
        deterministic_relation,
        deterministic_confidence,
        constraints,
        make_assessment("direct_revision", confidence=0.88),
    )

    assert deterministic_relation == "uncertain"
    assert constraints["parent_child_supported"] is True
    assert result[0] == "direct_revision"
    assert result[1] == "llm_supported"
    assert result[3] is False
