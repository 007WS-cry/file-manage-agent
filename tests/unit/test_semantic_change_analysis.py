from __future__ import annotations

from app.nodes.version_analysis import apply_subagent_summary
from app.services.recommendation import apply_semantic_review_rules
from app.services.semantic_change_analysis import (
    build_change_evidence,
    calculate_review_priority,
)
from app.state.models import VersionSubagentOutput

"""验证语义差异证据边界和确定性人工审核规则。"""


def _payment_change() -> dict:
    """构造一项付款期限高重要性语义变更。"""
    return {
        "change_type": "payment_term",
        "significance": "high",
        "old_value": "30天",
        "new_value": "60天",
        "business_impact": "回款周期延长",
        "evidence_refs": ["diff:comparison-001:paragraph-18-to-18"],
        "confidence": 0.94,
    }


def test_build_change_evidence_preserves_changed_paragraph_and_reference() -> None:
    """付款期限变化应形成有界 old/new 段落和稳定 diff 引用。"""
    old_text = "第一条 合同范围\n第十八条 付款期限为30天\n第十九条 其他约定"
    new_text = "第一条 合同范围\n第十八条 付款期限为60天\n第十九条 其他约定"

    evidence = build_change_evidence(
        "comparison-001",
        old_text,
        new_text,
        {},
        {},
    )

    assert evidence == [
        {
            "evidence_ref": "diff:comparison-001:paragraph-2-to-2",
            "source": "text_diff",
            "old_value": "第十八条 付款期限为30天",
            "new_value": "第十八条 付款期限为60天",
        }
    ]


def test_build_change_evidence_includes_structure_only_change() -> None:
    """正文相同但表格数量变化时应生成可供格式分类引用的结构证据。"""
    evidence = build_change_evidence(
        "comparison-structure",
        "合同正文",
        "合同正文",
        {},
        {},
        old_structure={"paragraph_count": 1, "table_count": 0},
        new_structure={"paragraph_count": 1, "table_count": 1},
    )

    assert evidence == [
        {
            "evidence_ref": "diff:comparison-structure:structure",
            "source": "structure",
            "old_value": '{"paragraph_count": 1, "table_count": 0}',
            "new_value": '{"paragraph_count": 1, "table_count": 1}',
        }
    ]


def test_rule_engine_maps_semantic_change_to_review_priority() -> None:
    """LLM 分类只作为输入，最终审核等级必须由固定规则映射。"""
    high_priority, high_reasons = calculate_review_priority([_payment_change()])
    wording_priority, wording_reasons = calculate_review_priority(
        [
            {
                "change_type": "wording_only",
                "significance": "high",
                "old_value": "一式两份",
                "new_value": "共两份",
                "business_impact": "不改变权利义务",
                "evidence_refs": ["diff:comparison-002:paragraph-3-to-3"],
                "confidence": 0.91,
            }
        ]
    )

    assert high_priority == "high"
    assert high_reasons == ["payment_term / high：回款周期延长"]
    assert wording_priority == "low"
    assert wording_reasons == []


def test_high_semantic_priority_forces_human_review() -> None:
    """高重要性变更必须覆盖原本可自动选择的推荐结果。"""
    decision = {
        "id": "decision:group-001",
        "group_id": "group-001",
        "candidate_scores": {"v1": 0.4, "v2": 0.95},
        "recommended_file_id": "v2",
        "reasons": ["规则推荐置信度充足"],
        "confidence": 0.96,
        "needs_human_review": False,
        "selected_by": "rule",
        "preserve_file_ids": ["v1", "v2"],
    }
    diff = {
        "id": "diff-001",
        "group_id": "group-001",
        "review_priority": "high",
    }

    updated = apply_semantic_review_rules(decision, [diff])

    assert updated["needs_human_review"] is True
    assert updated["selected_by"] == "unresolved"
    assert any("高重要性业务变更" in reason for reason in updated["reasons"])


def test_relation_conflict_forces_human_review() -> None:
    """双轨关系冲突必须由规则引擎升级为人工审核。"""
    decision = {
        "id": "decision:group-001",
        "group_id": "group-001",
        "candidate_scores": {"v1": 0.4, "v2": 0.95},
        "recommended_file_id": "v2",
        "reasons": ["规则推荐置信度充足"],
        "confidence": 0.96,
        "needs_human_review": False,
        "selected_by": "rule",
        "preserve_file_ids": ["v1", "v2"],
    }
    diff = {
        "id": "diff-001",
        "group_id": "group-001",
        "review_priority": "low",
        "relation_review_required": True,
    }

    updated = apply_semantic_review_rules(decision, [diff])

    assert updated["needs_human_review"] is True
    assert updated["selected_by"] == "unresolved"
    assert any("版本关系规则" in reason for reason in updated["reasons"])


def test_applying_version_output_runs_rule_engine_and_records_source() -> None:
    """成功语义输出写入 Diff 时应同步产生规则优先级和消息来源。"""
    state = {
        "current_diff": {
            "id": "comparison-001",
            "group_id": "group-001",
            "semantic_changes": [],
            "review_priority": "not_assessed",
            "review_reasons": [],
            "deterministic_relation": "direct_revision",
            "deterministic_relation_confidence": 0.86,
            "relation_constraints": {
                "exact_hash_match": False,
                "parent_child_supported": True,
                "export_supported": False,
                "semantic_duplicate_supported": False,
                "parallel_branch_supported": False,
                "unrelated_supported": False,
            },
            "llm_relation": None,
            "resolved_relation": "direct_revision",
            "relation_resolution": "deterministic_only",
            "relation_confidence": 0.86,
            "relation_review_required": False,
            "relation_review_reasons": ["确定性直接修订"],
            "summary": "确定性摘要",
            "summary_source": "deterministic",
            "summary_message_id": None,
            "summary_artifact_ref": None,
        },
        "current_version_subagent_input": {
            "task_id": "run-001:version_analysis",
            "artifact_refs": [],
        },
        "current_version_subagent_output": VersionSubagentOutput(
            summary="付款期限从30天延长到60天。",
            semantic_changes=[_payment_change()],
            artifact_refs=[],
        ),
        "team_messages": [
            {
                "message_id": "message-001",
                "task_id": "run-001:version_analysis",
                "sender": "version-subagent",
                "message_type": "result",
            }
        ],
    }

    update = apply_subagent_summary(state)
    diff = update["current_diff"]

    assert diff["summary_source"] == "version_subagent"
    assert diff["summary_message_id"] == "message-001"
    assert diff["semantic_changes"][0]["change_type"] == "payment_term"
    assert diff["review_priority"] == "high"
    assert diff["review_reasons"] == ["payment_term / high：回款周期延长"]
