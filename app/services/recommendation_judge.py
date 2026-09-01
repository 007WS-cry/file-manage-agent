from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

from app.services.business_evidence import excluded_business_evidence_file_ids
from app.state.models import (
    DecisionRecord,
    FileGovernanceState,
    RecommendationJudgeCandidateInput,
    RecommendationJudgeInput,
    RecommendationJudgeOutput,
    RecommendationJudgeRecord,
)

"""本模块构造 Recommendation Judge 压缩决策包，校验第二意见并用固定规则融合。"""

# Judge 意见被视为明确的最低置信度。
RECOMMENDATION_JUDGE_HIGH_CONFIDENCE_THRESHOLD = 0.80

# 低置信确定性场景中 Judge 对单个候选的最大固定加分。
RECOMMENDATION_JUDGE_MAX_PRIORITY_BOOST = 0.04

# 单个决策包允许进入 Judge 的最高分候选数量。
RECOMMENDATION_JUDGE_MAX_CANDIDATES = 8

# 单个候选允许携带的语义变更和证据摘要数量。
RECOMMENDATION_JUDGE_MAX_SUMMARIES_PER_CANDIDATE = 3

# 单个候选摘要允许的最大字符数。
RECOMMENDATION_JUDGE_MAX_SUMMARY_CHARACTERS = 160

# 单次 Judge 决策包允许携带的最大受控引用数量。
RECOMMENDATION_JUDGE_MAX_ARTIFACT_REFS = 20


def _bounded_unique_text(values: Iterable[object], *, limit: int) -> list[str]:
    """把摘要收敛为去重、有界且顺序稳定的文本列表。

    Args:
        values: 等待收敛的摘要值。
        limit: 最多保留的条目数。

    Returns:
        不含空值和重复项的文本列表。
    """
    bounded: list[str] = []
    for value in values:
        if len(bounded) >= limit:
            break
        text = str(value).strip()[:RECOMMENDATION_JUDGE_MAX_SUMMARY_CHARACTERS]
        if text and text not in bounded:
            bounded.append(text)
    return bounded


def _candidate_semantic_changes(
    state: FileGovernanceState,
    *,
    group_id: str,
    file_id: str,
) -> tuple[list[str], list[str]]:
    """压缩与一个候选相关的结构化语义变更。

    Args:
        state: 已完成确定性推荐的顶层状态。
        group_id: 候选所属版本组 ID。
        file_id: 等待收集摘要的文件 ID。

    Returns:
        不含旧值、新值或完整正文的语义变更摘要，以及对应受控引用。
    """
    summaries: list[str] = []
    refs: list[str] = []
    for diff in state.get("diffs", []):
        if diff.get("group_id") != group_id or file_id not in {
            diff.get("file_a_id"),
            diff.get("file_b_id"),
        }:
            continue
        for change in diff.get("semantic_changes", []):
            summaries.append(
                f"{change['change_type']} / {change['significance']} / "
                f"{change['business_impact']} / confidence={change['confidence']:.2f}"
            )
            refs.extend(change.get("evidence_refs", []))
    return (
        _bounded_unique_text(
            summaries,
            limit=RECOMMENDATION_JUDGE_MAX_SUMMARIES_PER_CANDIDATE,
        ),
        _bounded_unique_text(refs, limit=RECOMMENDATION_JUDGE_MAX_ARTIFACT_REFS),
    )


def _candidate_evidence(
    state: FileGovernanceState,
    *,
    group_id: str,
    file_id: str,
) -> tuple[list[str], list[str]]:
    """压缩一个候选的发送、PDF 和业务证据并收集受控引用。

    Args:
        state: 已完成 Evidence 和确定性推荐的顶层状态。
        group_id: 候选所属版本组 ID。
        file_id: 等待收集证据的文件 ID。

    Returns:
        有界证据摘要和对应的受控引用。
    """
    summaries: list[str] = []
    refs: list[str] = []
    for delivery in state.get("deliveries", []):
        if delivery.get("group_id") != group_id or delivery.get("file_id") != file_id:
            continue
        summaries.append(
            f"delivery:{delivery['match_method']} / "
            f"confirmed={delivery['customer_confirmed']} / "
            f"confidence={delivery['confidence']:.2f}"
        )
        refs.append(delivery["evidence_ref"])
    for evidence in state.get("business_evidence", []):
        if evidence.get("group_id") != group_id or evidence.get("target_file_id") != file_id:
            continue
        summaries.append(
            f"business:{evidence['evidence_type']} / {evidence['status']} / "
            f"confidence={evidence['confidence']:.2f} / {evidence['reason']}"
        )
        refs.extend(evidence.get("evidence_refs", []))
    for pdf_export in state.get("pdf_exports", []):
        if pdf_export.get("group_id") != group_id:
            continue
        if file_id not in {pdf_export.get("pdf_file_id"), pdf_export.get("source_file_id")}:
            continue
        summaries.append(
            f"pdf_source:score={pdf_export['match_score']:.2f} / "
            f"confidence={pdf_export['confidence']:.2f}"
        )
        refs.append(f"state://pdf_exports/{pdf_export['id']}")
    return (
        _bounded_unique_text(
            summaries,
            limit=RECOMMENDATION_JUDGE_MAX_SUMMARIES_PER_CANDIDATE,
        ),
        _bounded_unique_text(refs, limit=RECOMMENDATION_JUDGE_MAX_ARTIFACT_REFS),
    )


def _version_position(
    state: FileGovernanceState,
    *,
    group_id: str,
    file_id: str,
) -> str:
    """计算候选在当前版本链中的压缩位置。

    Args:
        state: 包含版本链的顶层状态。
        group_id: 候选所属版本组 ID。
        file_id: 候选文件 ID。

    Returns:
        ``leaf``、``non_leaf`` 或 ``unknown``。
    """
    chain = next(
        (item for item in state.get("version_chains", []) if item.get("group_id") == group_id),
        None,
    )
    if chain is None or file_id not in chain.get("ordered_file_ids", []):
        return "unknown"
    return "leaf" if file_id in chain.get("leaf_file_ids", []) else "non_leaf"


def _group_risk_flags(state: FileGovernanceState, group_id: str) -> list[str]:
    """从确定性状态收集一个版本组的有界风险标记。

    Args:
        state: 已完成 Version、Evidence 和 Recommendation 的顶层状态。
        group_id: 等待收集风险的版本组 ID。

    Returns:
        去重且不含文档正文的风险标记。
    """
    flags: list[str] = []
    if any(item.get("group_id") == group_id for item in state.get("branches", [])):
        flags.append("parallel_branch_present")
    chain = next(
        (item for item in state.get("version_chains", []) if item.get("group_id") == group_id),
        None,
    )
    if chain is None or chain.get("is_complete") is not True:
        flags.append("version_chain_incomplete")
    for diff in state.get("diffs", []):
        if diff.get("group_id") != group_id:
            continue
        if diff.get("relation_review_required") is True:
            flags.append("version_relation_review_required")
        if diff.get("review_priority") == "high":
            flags.append("high_significance_change")
    for evidence in state.get("business_evidence", []):
        if evidence.get("group_id") == group_id and evidence.get("rule_action") == "human_review":
            flags.append("business_evidence_review_required")
    return _bounded_unique_text(flags, limit=50)


def build_recommendation_judge_requests(
    state: FileGovernanceState,
) -> list[RecommendationJudgeInput]:
    """为每个确定性推荐构造不含完整文档的 Judge 决策包。

    Args:
        state: 已完成 Recommendation 子图的顶层治理状态。

    Returns:
        按版本组 ID 稳定排序的压缩决策包列表。
    """
    task_id = f"{state['run']['run_id']}:recommendation"
    requests: list[RecommendationJudgeInput] = []
    business_records = list(state.get("business_evidence", []))
    for decision in sorted(state.get("decisions", []), key=lambda item: item["group_id"]):
        group_id = decision["group_id"]
        deterministic_id = decision.get("recommended_file_id")
        excluded_ids = excluded_business_evidence_file_ids(
            business_records,
            group_id,
        )
        candidates: list[RecommendationJudgeCandidateInput] = []
        artifact_refs: list[str] = []
        ranked_candidates = [
            item
            for item in sorted(
                decision["candidate_scores"].items(),
                key=lambda item: (-item[1], item[0]),
            )
            if item[0] not in excluded_ids
        ]
        shortlisted_candidates = ranked_candidates[:RECOMMENDATION_JUDGE_MAX_CANDIDATES]
        if (
            deterministic_id is not None
            and deterministic_id not in {item[0] for item in shortlisted_candidates}
        ):
            deterministic_candidate = next(
                (item for item in ranked_candidates if item[0] == deterministic_id),
                None,
            )
            if deterministic_candidate is not None and shortlisted_candidates:
                shortlisted_candidates[-1] = deterministic_candidate
        for file_id, score in shortlisted_candidates:
            semantic_changes, semantic_refs = _candidate_semantic_changes(
                state,
                group_id=group_id,
                file_id=file_id,
            )
            evidence, evidence_refs = _candidate_evidence(
                state,
                group_id=group_id,
                file_id=file_id,
            )
            candidates.append(
                RecommendationJudgeCandidateInput(
                    file_id=file_id,
                    deterministic_score=score,
                    version_position=_version_position(
                        state,
                        group_id=group_id,
                        file_id=file_id,
                    ),
                    semantic_changes=semantic_changes,
                    evidence=evidence,
                )
            )
            artifact_refs.extend(semantic_refs)
            artifact_refs.extend(evidence_refs)
        candidate_ids = {item["file_id"] for item in candidates}
        requests.append(
            RecommendationJudgeInput(
                task_id=task_id,
                group_id=group_id,
                candidates=candidates,
                deterministic_recommended_file_id=(
                    deterministic_id if deterministic_id in candidate_ids else None
                ),
                deterministic_confidence=decision["confidence"],
                deterministic_needs_human_review=decision["needs_human_review"],
                risk_flags=_group_risk_flags(state, group_id),
                artifact_refs=_bounded_unique_text(
                    artifact_refs,
                    limit=RECOMMENDATION_JUDGE_MAX_ARTIFACT_REFS,
                ),
            )
        )
    return requests


def validate_recommendation_judge_output(
    input_data: RecommendationJudgeInput,
    output: RecommendationJudgeOutput,
) -> RecommendationJudgeOutput:
    """校验 Judge 弃权、候选 ID 和产物引用不越过决策包边界。

    Args:
        input_data: 已验证的压缩决策包。
        output: 已通过 Pydantic 结构校验的 Judge 输出。

    Returns:
        与 Provider 返回对象解除可变引用的结构化输出。

    Raises:
        ValueError: Judge 弃权协议、候选白名单或引用白名单被违反时抛出。
    """
    candidate_ids = {item["file_id"] for item in input_data["candidates"]}
    if output.should_abstain and output.recommended_file_id is not None:
        raise ValueError("Judge 弃权时 recommended_file_id 必须为 None")
    if not output.should_abstain and output.recommended_file_id not in candidate_ids:
        raise ValueError("Judge 非弃权输出必须选择一个输入候选")
    if any(ref not in input_data["artifact_refs"] for ref in output.artifact_refs):
        raise ValueError("Judge artifact_refs 包含决策包白名单之外的引用")
    return output.model_copy(deep=True)


def _judgment_id(group_id: str) -> str:
    """为版本组生成稳定的 Judge 融合记录 ID。

    Args:
        group_id: 版本组 ID。

    Returns:
        以 ``recommendation-judge-`` 为前缀的 SHA-256 稳定 ID。
    """
    return "recommendation-judge-" + hashlib.sha256(group_id.encode("utf-8")).hexdigest()


def fuse_recommendation_judgment(
    decision: DecisionRecord,
    output: RecommendationJudgeOutput,
) -> tuple[DecisionRecord, RecommendationJudgeRecord]:
    """用固定双通道规则融合确定性推荐和 Judge 第二意见。

    Judge 只能在原结果低置信时为一个候选增加有上限的优先级，不能清除
    已有人工审核标记。与高置信确定性结果冲突时必须强制人工审核。

    Args:
        decision: Judge 介入前的确定性推荐。
        output: 已通过候选和引用白名单校验的第二意见。

    Returns:
        融合后的推荐副本与独立 Judge 审计记录。
    """
    updated = dict(decision)
    scores = dict(decision["candidate_scores"])
    reasons = list(decision["reasons"])
    deterministic_id = decision.get("recommended_file_id")
    judge_id = output.recommended_file_id
    deterministic_high = deterministic_id is not None and decision["needs_human_review"] is False
    score_adjustment = 0.0

    if output.should_abstain:
        if deterministic_high:
            resolution = "deterministic_only"
        else:
            resolution = "abstained_review"
    elif judge_id == deterministic_id and deterministic_id is not None:
        if deterministic_high:
            resolution = (
                "strong_consensus"
                if output.confidence >= RECOMMENDATION_JUDGE_HIGH_CONFIDENCE_THRESHOLD
                else "weak_consensus"
            )
            reasons.append("Judge 与确定性结果一致；第二意见仅作为共识审计")
        elif output.confidence >= RECOMMENDATION_JUDGE_HIGH_CONFIDENCE_THRESHOLD:
            resolution = "judge_priority_signal"
            score_adjustment = round(
                RECOMMENDATION_JUDGE_MAX_PRIORITY_BOOST * output.confidence,
                4,
            )
            scores[judge_id] = round(min(1.0, scores[judge_id] + score_adjustment), 4)
            reasons.append(
                f"Judge 明确支持低置信候选 {judge_id}，优先级 +{score_adjustment:.2f}；"
                "仍保持人工审核"
            )
        else:
            resolution = "weak_consensus"
            reasons.append("Judge 低置信同意当前候选；不改变原人工审核要求")
    elif deterministic_high:
        resolution = "conflict_review"
        updated["needs_human_review"] = True
        updated["selected_by"] = "unresolved"
        reasons.append(
            f"Judge 候选 {judge_id} 与确定性候选 {deterministic_id} 冲突，固定规则强制人工审核"
        )
    elif (
        judge_id is not None and output.confidence >= RECOMMENDATION_JUDGE_HIGH_CONFIDENCE_THRESHOLD
    ):
        resolution = "judge_priority_signal"
        score_adjustment = round(
            RECOMMENDATION_JUDGE_MAX_PRIORITY_BOOST * output.confidence,
            4,
        )
        scores[judge_id] = round(min(1.0, scores[judge_id] + score_adjustment), 4)
        ranked_ids = sorted(scores, key=lambda file_id: (-scores[file_id], file_id))
        updated["recommended_file_id"] = ranked_ids[0] if ranked_ids else None
        updated["needs_human_review"] = True
        updated["selected_by"] = "unresolved"
        reasons.append(
            f"Judge 在低置信场景支持候选 {judge_id}，优先级 +{score_adjustment:.2f}；"
            "不允许因此自动通过"
        )
    else:
        resolution = "weak_consensus"
        updated["needs_human_review"] = True
        updated["selected_by"] = "unresolved"
        reasons.append("Judge 意见置信度不足，不调整候选优先级")

    updated["candidate_scores"] = scores
    updated["reasons"] = list(dict.fromkeys(reasons))
    fused_decision = DecisionRecord(**updated)
    record = RecommendationJudgeRecord(
        id=_judgment_id(decision["group_id"]),
        group_id=decision["group_id"],
        deterministic_recommended_file_id=deterministic_id,
        deterministic_confidence=decision["confidence"],
        judge_recommended_file_id=judge_id,
        judge_confidence=output.confidence,
        supporting_reasons=list(output.supporting_reasons),
        counterarguments=list(output.counterarguments),
        missing_information=list(output.missing_information),
        should_abstain=output.should_abstain,
        resolution=resolution,
        review_required=fused_decision["needs_human_review"],
        score_adjustment=score_adjustment,
    )
    return fused_decision, record


def fuse_recommendation_judgments(
    decisions: Sequence[DecisionRecord],
    outputs_by_group: dict[str, RecommendationJudgeOutput],
) -> tuple[list[DecisionRecord], list[RecommendationJudgeRecord]]:
    """按版本组稳定融合一批 Judge 意见。

    Args:
        decisions: 确定性 Recommendation 子图产生的推荐列表。
        outputs_by_group: 版本组 ID 到已校验 Judge 输出的映射。

    Returns:
        顺序不变的融合推荐列表与 Judge 审计记录列表。
    """
    fused: list[DecisionRecord] = []
    records: list[RecommendationJudgeRecord] = []
    for decision in decisions:
        output = outputs_by_group.get(decision["group_id"])
        if output is None:
            fused.append(DecisionRecord(**dict(decision)))
            continue
        fused_decision, record = fuse_recommendation_judgment(decision, output)
        fused.append(fused_decision)
        records.append(record)
    return fused, records
