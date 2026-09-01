from __future__ import annotations

from copy import deepcopy

import pytest

from app.agents.protocol import TeamProtocolError, validate_recommendation_judge_input
from app.services.recommendation_judge import (
    RECOMMENDATION_JUDGE_MAX_CANDIDATES,
    RECOMMENDATION_JUDGE_MAX_PRIORITY_BOOST,
    RECOMMENDATION_JUDGE_MAX_SUMMARY_CHARACTERS,
    build_recommendation_judge_requests,
    fuse_recommendation_judgment,
    validate_recommendation_judge_output,
)
from app.state.models import DecisionRecord, RecommendationJudgeOutput

"""本文件验证 Recommendation Judge 的决策包边界、弃权协议和确定性双通道融合。"""

# 测试决策包使用的受控证据引用。
ALLOWED_JUDGE_REF = "diff:contract-a-b:paragraph-18"


def _judge_input() -> dict:
    """创建一个包含两个候选的最小 Judge 决策包。

    Returns:
        可直接交给 Team Protocol 校验的决策包。
    """
    return {
        "task_id": "run-judge:recommendation",
        "group_id": "contract-group-01",
        "candidates": [
            {
                "file_id": "v3",
                "deterministic_score": 0.83,
                "version_position": "leaf",
                "semantic_changes": ["付款期限 / high / 回款周期延长"],
                "evidence": [],
            },
            {
                "file_id": "v4",
                "deterministic_score": 0.81,
                "version_position": "leaf",
                "semantic_changes": [],
                "evidence": ["明确最终版本 / confidence=0.92"],
            },
        ],
        "deterministic_recommended_file_id": "v3",
        "deterministic_confidence": 0.86,
        "deterministic_needs_human_review": False,
        "risk_flags": [],
        "artifact_refs": [ALLOWED_JUDGE_REF],
    }


def _decision(*, needs_review: bool = False) -> DecisionRecord:
    """创建固定融合测试使用的确定性推荐。

    Args:
        needs_review: 确定性规则是否已要求人工审核。

    Returns:
        候选为 v3 和 v4 的完整推荐记录。
    """
    return DecisionRecord(
        id="decision-contract-group-01",
        group_id="contract-group-01",
        candidate_scores={"v3": 0.83, "v4": 0.81},
        recommended_file_id="v3",
        reasons=["确定性基线"],
        confidence=0.78 if needs_review else 0.86,
        needs_human_review=needs_review,
        selected_by="unresolved" if needs_review else "rule",
        preserve_file_ids=["v3", "v4"],
    )


def _judge_output(
    file_id: str | None,
    *,
    confidence: float,
    abstain: bool = False,
) -> RecommendationJudgeOutput:
    """创建一个有界 Judge 结构化输出。

    Args:
        file_id: Judge 候选；弃权时为 None。
        confidence: Judge 置信度。
        abstain: 是否明确弃权。

    Returns:
        可交给白名单校验或融合函数的 Pydantic 输出。
    """
    return RecommendationJudgeOutput(
        summary="Judge 结构化第二意见。",
        recommended_file_id=file_id,
        confidence=confidence,
        supporting_reasons=["受控决策包支持该候选。"] if file_id else [],
        counterarguments=[],
        missing_information=[] if file_id else ["关键证据不足。"],
        should_abstain=abstain,
        artifact_refs=[],
    )


def test_protocol_rejects_full_content_and_candidate_escape() -> None:
    """Judge 输入应拒绝完整正文字段和候选集外的确定性 ID。"""
    full_content_payload = _judge_input()
    full_content_payload["full_content"] = "完整合同正文"
    with pytest.raises(TeamProtocolError, match="协议外字段"):
        validate_recommendation_judge_input(full_content_payload)

    escaped_candidate_payload = _judge_input()
    escaped_candidate_payload["deterministic_recommended_file_id"] = "v5"
    with pytest.raises(TeamProtocolError, match="必须属于 candidates"):
        validate_recommendation_judge_input(escaped_candidate_payload)


def test_decision_package_is_bounded_and_keeps_deterministic_candidate() -> None:
    """决策包应限制候选和摘要，同时保留确定性候选与语义证据引用。"""
    state = {
        "run": {"run_id": "run-bounded-judge"},
        "decisions": [
            _decision(needs_review=True)
            | {
                "candidate_scores": {f"v{index}": 1.0 - index / 20 for index in range(12)},
                "recommended_file_id": "v11",
            }
        ],
        "business_evidence": [],
        "deliveries": [],
        "pdf_exports": [],
        "branches": [],
        "version_chains": [],
        "diffs": [
            {
                "group_id": "contract-group-01",
                "file_a_id": "v0",
                "file_b_id": "v1",
                "relation_review_required": False,
                "review_priority": "high",
                "semantic_changes": [
                    {
                        "change_type": "payment_term",
                        "significance": "high",
                        "business_impact": "回款周期延长" * 100,
                        "confidence": 0.94,
                        "evidence_refs": [ALLOWED_JUDGE_REF],
                    }
                ],
            }
        ],
    }

    [request] = build_recommendation_judge_requests(state)  # type: ignore[arg-type]

    assert len(request["candidates"]) == RECOMMENDATION_JUDGE_MAX_CANDIDATES
    assert "v11" in {item["file_id"] for item in request["candidates"]}
    assert max(
        len(summary)
        for item in request["candidates"]
        for summary in item["semantic_changes"]
    ) <= RECOMMENDATION_JUDGE_MAX_SUMMARY_CHARACTERS
    assert ALLOWED_JUDGE_REF in request["artifact_refs"]


def test_output_validation_enforces_abstention_and_candidate_whitelist() -> None:
    """Judge 弃权时必须无候选，非弃权时必须选择输入候选。"""
    input_data = validate_recommendation_judge_input(_judge_input())
    with pytest.raises(ValueError, match="弃权"):
        validate_recommendation_judge_output(
            input_data,
            _judge_output("v3", confidence=0.2, abstain=True),
        )
    with pytest.raises(ValueError, match="输入候选"):
        validate_recommendation_judge_output(
            input_data,
            _judge_output("v5", confidence=0.9),
        )


def test_high_confidence_agreement_records_strong_consensus() -> None:
    """高置信一致应保留自动结果并记录强共识。"""
    original = _decision()

    fused, record = fuse_recommendation_judgment(
        original,
        _judge_output("v3", confidence=0.91),
    )

    assert fused["recommended_file_id"] == original["recommended_file_id"]
    assert fused["candidate_scores"] == original["candidate_scores"]
    assert fused["needs_human_review"] is False
    assert fused["reasons"][-1].startswith("Judge 与确定性结果一致")
    assert record["resolution"] == "strong_consensus"
    assert record["review_required"] is False
    assert record["score_adjustment"] == 0.0


def test_conflict_forces_review_without_switching_deterministic_candidate() -> None:
    """两路候选冲突应强制审核，不得直接切换到 Judge 候选。"""
    original = _decision()
    original_snapshot = deepcopy(original)

    fused, record = fuse_recommendation_judgment(
        original,
        _judge_output("v4", confidence=0.93),
    )

    assert original == original_snapshot
    assert fused["recommended_file_id"] == "v3"
    assert fused["candidate_scores"] == original["candidate_scores"]
    assert fused["needs_human_review"] is True
    assert fused["selected_by"] == "unresolved"
    assert record["resolution"] == "conflict_review"


def test_low_confidence_deterministic_result_only_receives_bounded_priority_signal() -> None:
    """低置信场景只应获得有上限优先级信号，不得自动通过。"""
    original = _decision(needs_review=True)

    fused, record = fuse_recommendation_judgment(
        original,
        _judge_output("v4", confidence=1.0),
    )

    assert fused["candidate_scores"]["v4"] == pytest.approx(
        0.81 + RECOMMENDATION_JUDGE_MAX_PRIORITY_BOOST
    )
    assert fused["recommended_file_id"] == "v4"
    assert fused["needs_human_review"] is True
    assert fused["selected_by"] == "unresolved"
    assert record["resolution"] == "judge_priority_signal"
    assert record["score_adjustment"] == RECOMMENDATION_JUDGE_MAX_PRIORITY_BOOST


def test_abstention_preserves_deterministic_decision_exactly() -> None:
    """Judge 安全弃权不应改变现有确定性推荐内容。"""
    original = _decision()

    fused, record = fuse_recommendation_judgment(
        original,
        _judge_output(None, confidence=0.0, abstain=True),
    )

    assert fused == original
    assert record["resolution"] == "deterministic_only"
    assert record["should_abstain"] is True
