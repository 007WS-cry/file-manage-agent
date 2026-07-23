from __future__ import annotations

from pathlib import Path

from app.graphs.file_governance import file_governance_graph
from app.graphs.recommendation import recommendation_graph
from app.nodes.subgraphs_nodes import run_recommendation_subgraph
from app.state.factories import create_initial_state
from app.state.models import FileRecord, RecommendationGraphState

"""本文件集成测试独立 Recommendation 子图的规则链、状态隔离和审核输出。"""


def make_file_record(
    file_id: str,
    file_name: str,
    extension: str,
    modified_at: str,
) -> FileRecord:
    """构造 Recommendation 子图测试使用的已解析非重复文件。

    Args:
        file_id: 测试使用的稳定文件 ID。
        file_name: 用于最终版标记和解释的文件名。
        extension: 包含前导点的文件扩展名。
        modified_at: 带时区的 ISO 8601 修改时间。

    Returns:
        不需要访问真实文件内容的文件状态记录。
    """
    return FileRecord(
        id=file_id,
        absolute_path=f"/readonly/{file_name}",
        file_name=file_name,
        normalized_stem="合同",
        extension=extension,
        size_bytes=1,
        modified_at=modified_at,
        sha256=(file_id[0] if file_id else "0") * 64,
        duplicate_of=None,
        parse_status="parsed",
        parse_error=None,
    )


def make_recommendation_state(
    *,
    with_branch: bool = False,
    with_evidence: bool = True,
) -> RecommendationGraphState:
    """构造包含线性链或分叉以及可选外部证据的子图状态。

    Args:
        with_branch: 是否加入会强制人工审核的版本分叉。
        with_evidence: 是否加入客户确认和 PDF 来源证据。

    Returns:
        可直接提交给独立 Recommendation 图的完整状态。
    """
    files = [
        make_file_record(
            "v1",
            "合同_v1.docx",
            ".docx",
            "2026-01-01T00:00:00+00:00",
        ),
        make_file_record(
            "source",
            "合同_最终版.docx",
            ".docx",
            "2026-01-02T00:00:00+00:00",
        ),
        make_file_record(
            "pdf",
            "合同.pdf",
            ".pdf",
            "2026-01-03T00:00:00+00:00",
        ),
    ]
    branches = (
        [
            {
                "id": "branch:group:v1",
                "group_id": "group",
                "root_file_id": "v1",
                "child_file_ids": ["source", "pdf"],
                "reason": "共同父版本产生两个叶子",
                "confidence": 0.95,
            }
        ]
        if with_branch
        else []
    )
    deliveries = (
        [
            {
                "id": "delivery:source",
                "group_id": "group",
                "file_id": "source",
                "evidence_source": "local_log",
                "sent_at": "2026-01-04T00:00:00+00:00",
                "recipient_label": "客户A",
                "evidence_ref": "local-log:source",
                "match_method": "sha256",
                "customer_confirmed": True,
                "confidence": 1.0,
            }
        ]
        if with_evidence
        else []
    )
    pdf_exports = (
        [
            {
                "id": "pdf-export:pdf",
                "group_id": "group",
                "pdf_file_id": "pdf",
                "source_file_id": "source",
                "match_score": 1.0,
                "matched_signals": ["标准化内容一致"],
                "confidence": 1.0,
            }
        ]
        if with_evidence
        else []
    )
    return RecommendationGraphState(
        request={
            "root_directory": "/readonly",
            "recursive": True,
            "allowed_extensions": [".docx", ".pdf"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": False,
        },
        files=files,
        version_groups=[
            {
                "id": "group",
                "label": "合同",
                "file_ids": ["v1", "source", "pdf"],
                "grouping_signals": ["测试版本组"],
                "confidence": 0.95,
            }
        ],
        diffs=[],
        version_edges=[],
        branches=branches,
        version_chains=[
            {
                "id": "chain:group",
                "group_id": "group",
                "ordered_file_ids": ["v1", "source", "pdf"],
                "leaf_file_ids": ["source", "pdf"] if with_branch else ["source"],
                "is_complete": True,
                "warnings": [],
            }
        ],
        pdf_exports=pdf_exports,
        deliveries=deliveries,
        candidate_sets=[],
        decisions=[],
        human_review={
            "pending_group_ids": [],
            "selections": {},
            "review_note": "保留审核备注",
        },
        errors=[],
    )


def test_recommendation_graph_applies_evidence_and_auto_selects_source() -> None:
    """完整链和强外部证据应自动选择 PDF 的可编辑来源版本。"""
    result = recommendation_graph.invoke(make_recommendation_state())

    assert len(result["candidate_sets"]) == 1
    assert result["candidate_sets"][0]["editable_leaf_file_ids"] == ["source"]
    assert len(result["decisions"]) == 1
    decision = result["decisions"][0]
    assert decision["recommended_file_id"] == "source"
    assert decision["selected_by"] == "rule"
    assert decision["needs_human_review"] is False
    assert set(decision["preserve_file_ids"]) == {"v1", "source", "pdf"}
    assert any("客户已确认" in reason for reason in decision["reasons"])
    assert any("PDF 来源证据" in reason for reason in decision["reasons"])
    assert result["human_review"]["pending_group_ids"] == []
    assert result["errors"] == []


def test_recommendation_graph_branch_forces_human_review() -> None:
    """版本分叉必须覆盖高分和强证据，强制当前版本组进入人工审核。"""
    result = recommendation_graph.invoke(
        make_recommendation_state(with_branch=True),
    )

    decision = result["decisions"][0]
    assert decision["recommended_file_id"] == "source"
    assert decision["selected_by"] == "unresolved"
    assert decision["needs_human_review"] is True
    assert result["human_review"]["pending_group_ids"] == ["group"]
    assert any("版本分叉" in reason for reason in decision["reasons"])


def test_recommendation_graph_accepts_empty_business_state() -> None:
    """没有版本组时子图仍应从 START 到达 END 并返回空业务结果。"""
    state = make_recommendation_state(with_evidence=False)
    state["files"] = []
    state["version_groups"] = []
    state["version_chains"] = []

    result = recommendation_graph.invoke(state)

    assert result["candidate_sets"] == []
    assert result["decisions"] == []
    assert result["human_review"]["pending_group_ids"] == []
    assert result["errors"] == []


def test_recommendation_wrapper_filters_private_state_and_remains_independent(
    tmp_path: Path,
) -> None:
    """接入顶层图后包装节点仍应只返回白名单字段并保留独立子图。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    source_state = make_recommendation_state()
    top_state = create_initial_state(
        source_state["request"],
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(tmp_path / "artifacts"),
            "report_root": str(tmp_path / "reports"),
        },
    )
    for field in (
        "files",
        "version_groups",
        "diffs",
        "version_edges",
        "branches",
        "version_chains",
        "pdf_exports",
        "deliveries",
    ):
        top_state[field] = source_state[field]

    update = run_recommendation_subgraph(top_state)

    assert set(update) == {"memory", "decisions", "human_review", "errors"}
    assert update["decisions"][0]["recommended_file_id"] == "source"
    assert "candidate_sets" not in update
    assert "run_recommendation_subgraph" in file_governance_graph.get_graph().nodes
    assert "calculate_decision_confidence" in recommendation_graph.get_graph().nodes
