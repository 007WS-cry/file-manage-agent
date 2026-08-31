from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence

from app.state.models import (
    BusinessEvidenceRecord,
    BusinessEvidenceRuleAction,
    BusinessEvidenceStatus,
    BusinessEvidenceType,
    ControlledEvidenceSnippet,
    DecisionRecord,
    DeliveryRecord,
    EvidenceSubagentOutput,
)

"""本模块校验 Evidence 语义候选，并用确定性规则执行加权、排除与人工审核。"""

# 业务证据类型与规范化状态的一一对应关系，模型不能自由组合。
BUSINESS_EVIDENCE_STATUS_BY_TYPE: dict[BusinessEvidenceType, BusinessEvidenceStatus] = {
    "approved": "approved",
    "rejected": "rejected",
    "superseded": "superseded",
    "for_reference_only": "reference_only",
    "requires_revision": "revision_required",
    "final_version": "final",
    "sent_but_unconfirmed": "unconfirmed",
    "ambiguous": "ambiguous",
}

# 每种业务证据由规则引擎执行的主动作；模型输出中不存在该字段。
BUSINESS_EVIDENCE_RULE_ACTION_BY_TYPE: dict[
    BusinessEvidenceType,
    BusinessEvidenceRuleAction,
] = {
    "approved": "score_boost",
    "rejected": "exclude_and_review",
    "superseded": "exclude_candidate",
    "for_reference_only": "exclude_candidate",
    "requires_revision": "force_human_review",
    "final_version": "score_boost",
    "sent_but_unconfirmed": "none",
    "ambiguous": "none",
}

# 候选加权动作的固定最大增量；实际增量再乘经过校验的模型置信度。
BUSINESS_EVIDENCE_SCORE_ADJUSTMENT_BY_TYPE: dict[BusinessEvidenceType, float] = {
    "approved": 0.18,
    "final_version": 0.14,
}

# 会使目标文件退出主版本竞争的固定业务证据类型。
BUSINESS_EVIDENCE_EXCLUSION_TYPES = frozenset(
    {"rejected", "superseded", "for_reference_only"}
)

# 无论候选分数如何都必须转人工审核的固定业务证据类型。
BUSINESS_EVIDENCE_REVIEW_TYPES = frozenset({"rejected", "requires_revision"})

# 同一文件同时出现下列肯定与否定证据时必须交由人工处理。
BUSINESS_EVIDENCE_POSITIVE_TYPES = frozenset({"approved", "final_version"})

# 同一文件与肯定证据冲突的业务证据类型。
BUSINESS_EVIDENCE_NEGATIVE_TYPES = frozenset(
    {"rejected", "superseded", "for_reference_only", "requires_revision"}
)


def validate_business_evidence_analyses(
    output: EvidenceSubagentOutput,
    snippets: Sequence[ControlledEvidenceSnippet],
) -> None:
    """校验模型业务证据候选只能复用输入摘录中的文件、时间和引用。

    Args:
        output: 已通过 Pydantic Schema 校验的 Evidence Subagent 输出。
        snippets: 当前 Evidence 输入中经过 Team Protocol 校验的受控摘录。

    Raises:
        ValueError: 状态映射、引用、目标文件、时间或候选唯一性不符合协议时抛出。
    """
    snippet_by_ref = {item["evidence_ref"]: item for item in snippets}
    seen_keys: set[tuple[str, str, tuple[str, ...]]] = set()
    for index, analysis in enumerate(output.business_evidence):
        expected_status = BUSINESS_EVIDENCE_STATUS_BY_TYPE[analysis.evidence_type]
        if analysis.status != expected_status:
            raise ValueError(
                f"business_evidence[{index}].status 与 evidence_type 不一致"
            )
        unknown_refs = [
            item for item in analysis.evidence_refs if item not in snippet_by_ref
        ]
        if unknown_refs:
            raise ValueError(
                f"business_evidence[{index}] 引用了输入白名单之外的证据"
            )
        referenced_snippets = [
            snippet_by_ref[item] for item in analysis.evidence_refs
        ]
        if any(
            item["target_file_id"] != analysis.target_file_id
            for item in referenced_snippets
        ):
            raise ValueError(
                f"business_evidence[{index}].target_file_id 与所引摘录不一致"
            )
        if analysis.effective_time is not None and analysis.effective_time not in {
            item["effective_time"] for item in referenced_snippets
        }:
            raise ValueError(
                f"business_evidence[{index}].effective_time 不来自所引摘录"
            )
        key = (
            analysis.evidence_type,
            analysis.target_file_id,
            tuple(sorted(analysis.evidence_refs)),
        )
        if key in seen_keys:
            raise ValueError("business_evidence 不得包含重复语义候选")
        seen_keys.add(key)


def build_business_evidence_records(
    group_id: str,
    output: EvidenceSubagentOutput,
) -> list[BusinessEvidenceRecord]:
    """把已验证模型候选转换为带固定规则动作的顶层业务证据记录。

    Args:
        group_id: 当前 Evidence 分派所属版本组 ID。
        output: 已通过 Schema、引用和事实落点校验的模型输出。

    Returns:
        ID 稳定且动作、分值完全由代码映射生成的业务证据记录。
    """
    records: list[BusinessEvidenceRecord] = []
    for analysis in output.business_evidence:
        identity = json.dumps(
            {
                "group_id": group_id,
                "target_file_id": analysis.target_file_id,
                "evidence_type": analysis.evidence_type,
                "evidence_refs": sorted(analysis.evidence_refs),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        record_id = "business-evidence:" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()
        records.append(
            BusinessEvidenceRecord(
                id=record_id,
                group_id=group_id,
                evidence_type=analysis.evidence_type,
                target_file_id=analysis.target_file_id,
                status=analysis.status,
                actor_role=analysis.actor_role,
                effective_time=analysis.effective_time,
                reason=analysis.reason,
                evidence_refs=list(analysis.evidence_refs),
                confidence=analysis.confidence,
                rule_action=BUSINESS_EVIDENCE_RULE_ACTION_BY_TYPE[
                    analysis.evidence_type
                ],
                score_adjustment=BUSINESS_EVIDENCE_SCORE_ADJUSTMENT_BY_TYPE.get(
                    analysis.evidence_type,
                    0.0,
                ),
            )
        )
    return records


def excluded_business_evidence_file_ids(
    records: Iterable[BusinessEvidenceRecord],
    group_id: str,
) -> set[str]:
    """返回被固定业务证据规则排除出主版本竞争的文件 ID。

    Args:
        records: 当前运行已经固化的业务证据记录。
        group_id: 等待检查的版本组 ID。

    Returns:
        因拒绝、作废或仅供参考而不参与主版本竞争的文件 ID 集合。
    """
    return {
        item["target_file_id"]
        for item in records
        if item["group_id"] == group_id
        and item["evidence_type"] in BUSINESS_EVIDENCE_EXCLUSION_TYPES
    }


def apply_business_evidence_scoring(
    decision: DecisionRecord,
    records: Iterable[BusinessEvidenceRecord],
    deliveries: Iterable[DeliveryRecord],
) -> DecisionRecord:
    """用固定规则对业务证据目标加权或将其排除出主版本竞争。

    ``approved`` 的最大增量为 0.18，``final_version`` 的最大增量为 0.14，
    并乘模型置信度。同一引用已经通过 ``customer_confirmed`` 布尔事实获得发送
    规则加权时不会重复加分。拒绝、作废和仅供参考证据把目标分数置零，最终
    选择节点还会显式跳过这些目标。

    Args:
        decision: 已应用发送和 PDF 确定性证据的候选评分。
        records: 已由固定映射固化的业务证据记录。
        deliveries: 用于识别已加权客户确认事实的发送证据。

    Returns:
        应用业务证据固定评分与排除规则后的推荐记录副本。
    """
    updated = dict(decision)
    scores = dict(decision["candidate_scores"])
    reasons = list(decision["reasons"])
    group_records = [
        item for item in records if item["group_id"] == decision["group_id"]
    ]
    confirmed_refs_by_file: dict[str, set[str]] = {}
    for delivery in deliveries:
        file_id = delivery.get("file_id")
        if file_id is not None and delivery.get("customer_confirmed", False):
            confirmed_refs_by_file.setdefault(file_id, set()).add(
                delivery["evidence_ref"]
            )

    for item in group_records:
        file_id = item["target_file_id"]
        if file_id not in scores or item["rule_action"] != "score_boost":
            continue
        already_confirmed = (
            item["evidence_type"] == "approved"
            and bool(
                set(item["evidence_refs"])
                & confirmed_refs_by_file.get(file_id, set())
            )
        )
        if already_confirmed:
            reasons.append(
                f"业务证据：文件 {file_id} 的明确批准已由客户确认事实加权，未重复计分"
            )
            continue
        boost = round(item["score_adjustment"] * item["confidence"], 4)
        scores[file_id] = round(min(1.0, scores[file_id] + boost), 4)
        reasons.append(
            f"业务证据：文件 {file_id} 被识别为 {item['evidence_type']}，"
            f"候选分 +{boost:.2f}"
        )

    excluded_ids = excluded_business_evidence_file_ids(
        group_records,
        decision["group_id"],
    )
    for file_id in sorted(excluded_ids & set(scores)):
        scores[file_id] = 0.0
        evidence_types = sorted(
            {
                item["evidence_type"]
                for item in group_records
                if item["target_file_id"] == file_id
                and item["evidence_type"] in BUSINESS_EVIDENCE_EXCLUSION_TYPES
            }
        )
        reasons.append(
            f"业务证据规则：文件 {file_id} 因 {', '.join(evidence_types)} "
            "退出主版本竞争"
        )
    updated["candidate_scores"] = scores
    updated["reasons"] = list(dict.fromkeys(reasons))
    return DecisionRecord(**updated)


def apply_business_evidence_review(
    decision: DecisionRecord,
    records: Iterable[BusinessEvidenceRecord],
) -> DecisionRecord:
    """按固定规则把修改要求、拒绝或互相冲突的证据升级为人工审核。

    Args:
        decision: 已计算常规推荐置信度的推荐记录。
        records: 当前运行已经固化的业务证据记录。

    Returns:
        必要时强制设为 unresolved 并追加可审计规则理由的推荐记录。
    """
    updated = dict(decision)
    group_records = [
        item for item in records if item["group_id"] == decision["group_id"]
    ]
    force_types = sorted(
        {
            item["evidence_type"]
            for item in group_records
            if item["evidence_type"] in BUSINESS_EVIDENCE_REVIEW_TYPES
        }
    )
    types_by_file: dict[str, set[BusinessEvidenceType]] = {}
    for item in group_records:
        types_by_file.setdefault(item["target_file_id"], set()).add(
            item["evidence_type"]
        )
    conflict_file_ids = sorted(
        file_id
        for file_id, evidence_types in types_by_file.items()
        if evidence_types & BUSINESS_EVIDENCE_POSITIVE_TYPES
        and evidence_types & BUSINESS_EVIDENCE_NEGATIVE_TYPES
    )
    all_candidates_excluded = bool(decision["candidate_scores"]) and not (
        set(decision["candidate_scores"])
        - excluded_business_evidence_file_ids(group_records, decision["group_id"])
    )
    if force_types or conflict_file_ids or all_candidates_excluded:
        updated["needs_human_review"] = True
        updated["selected_by"] = "unresolved"
        reasons = list(decision["reasons"])
        if force_types:
            reasons.append(
                "业务证据规则：存在 "
                f"{', '.join(force_types)} 证据，强制进入人工审核"
            )
        if conflict_file_ids:
            reasons.append(
                "业务证据规则：同一文件同时存在肯定与否定证据，"
                "强制进入人工审核"
            )
        if all_candidates_excluded:
            reasons.append("业务证据规则：全部候选均被排除，必须由人工处理")
        updated["reasons"] = list(dict.fromkeys(reasons))
    return DecisionRecord(**updated)


def group_has_business_evidence_review(
    records: Iterable[BusinessEvidenceRecord],
    group_id: str,
) -> bool:
    """判断版本组是否含应优先展示的强制人工审核业务证据。

    Args:
        records: 当前运行全部业务证据记录。
        group_id: 等待检查的版本组 ID。

    Returns:
        存在拒绝、继续修改或同文件正负冲突证据时返回 True。
    """
    group_records = [item for item in records if item["group_id"] == group_id]
    if any(
        item["evidence_type"] in BUSINESS_EVIDENCE_REVIEW_TYPES
        for item in group_records
    ):
        return True
    types_by_file: dict[str, set[BusinessEvidenceType]] = {}
    for item in group_records:
        types_by_file.setdefault(item["target_file_id"], set()).add(
            item["evidence_type"]
        )
    return any(
        evidence_types & BUSINESS_EVIDENCE_POSITIVE_TYPES
        and evidence_types & BUSINESS_EVIDENCE_NEGATIVE_TYPES
        for evidence_types in types_by_file.values()
    )
