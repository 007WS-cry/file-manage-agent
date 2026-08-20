from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.services.memory_policy import capture_human_choice_memory
from app.services.recommendation import apply_human_selection as apply_human_selection_service
from app.services.semantic_change_analysis import (
    REVIEW_PRIORITY_RANK,
    highest_group_review_priority,
)
from app.services.version_relation_fusion import group_has_relation_review
from app.state.models import FileGovernanceState

"""本模块实现主版本人工审核的暂停与恢复，并保持既有 selections 输入协议。"""


def prepare_human_review(state: FileGovernanceState) -> dict:
    """在执行 interrupt 前把运行状态持久化为等待人工确认。"""
    run = dict(state["run"])
    run.update({"status": "waiting_human", "current_stage": "human_review"})
    pending_ids = sorted(
        (
            decision["group_id"]
            for decision in state.get("decisions", [])
            if decision["needs_human_review"]
        ),
        key=lambda group_id: (
            -int(group_has_relation_review(state.get("diffs", []), group_id)),
            -REVIEW_PRIORITY_RANK[
                highest_group_review_priority(state.get("diffs", []), group_id)
            ],
            group_id,
        ),
    )
    review = dict(state["human_review"])
    review["pending_group_ids"] = pending_ids
    return {"run": run, "human_review": review}


def request_human_review(state: FileGovernanceState) -> dict:
    """暂停顶层图并请求用户为每个待审核版本组选择一个主版本。

    interrupt 载荷只包含文件 ID、文件名、评分和推荐理由，不包含文档正文。
    恢复值必须是 ``{"selections": {group_id: file_id}, "review_note": ...}``，
    且每个待确认版本组都必须选择该组内的一个文件。
    本节点只处理 ``file_governance_review``，不会把 Error Recovery 的
    ``action`` 输入混入既有主版本选择协议。

    Args:
        state: 已标记为 ``waiting_human`` 的顶层治理状态。

    Returns:
        经过结构和成员关系校验的人工选择及可选说明。

    Raises:
        ValueError: 恢复值格式错误、缺少版本组或选择组外文件时抛出。
    """
    pending_ids = list(state["human_review"]["pending_group_ids"])
    group_by_id = {item["id"]: item for item in state.get("version_groups", [])}
    file_by_id = {item["id"]: item for item in state.get("files", [])}
    decision_by_group = {
        item["group_id"]: item for item in state.get("decisions", [])
    }
    review_groups = []
    for group_id in pending_ids:
        group = group_by_id[group_id]
        decision = decision_by_group[group_id]
        group_diffs = [
            diff for diff in state.get("diffs", []) if diff["group_id"] == group_id
        ]
        semantic_changes = [
            {
                "change_type": change["change_type"],
                "significance": change["significance"],
                "old_value": (
                    str(change.get("old_value"))[:300]
                    if change.get("old_value") is not None
                    else None
                ),
                "new_value": (
                    str(change.get("new_value"))[:300]
                    if change.get("new_value") is not None
                    else None
                ),
                "business_impact": change["business_impact"],
                "evidence_refs": list(change["evidence_refs"]),
                "confidence": change["confidence"],
            }
            for diff in group_diffs
            for change in diff.get("semantic_changes", [])
        ][:20]
        relation_assessments = [
            {
                "comparison_id": diff["id"],
                "deterministic_relation": diff.get(
                    "deterministic_relation",
                    "uncertain",
                ),
                "llm_relation": (
                    dict(diff["llm_relation"])
                    if diff.get("llm_relation") is not None
                    else None
                ),
                "resolved_relation": diff.get("resolved_relation", "uncertain"),
                "resolution": diff.get(
                    "relation_resolution",
                    "deterministic_only",
                ),
                "confidence": diff.get("relation_confidence", 0.0),
                "review_required": diff.get("relation_review_required", False),
                "review_reasons": list(
                    diff.get("relation_review_reasons", [])
                )[:5],
            }
            for diff in group_diffs
        ][:20]
        review_groups.append(
            {
                "group_id": group_id,
                "label": group["label"],
                "confidence": decision["confidence"],
                "semantic_review_priority": highest_group_review_priority(
                    group_diffs,
                    group_id,
                ),
                "semantic_changes": semantic_changes,
                "relation_review_required": group_has_relation_review(
                    group_diffs,
                    group_id,
                ),
                "relation_assessments": relation_assessments,
                "recommended_file_id": decision["recommended_file_id"],
                "reasons": decision["reasons"],
                "candidates": [
                    {
                        "file_id": file_id,
                        "file_name": file_by_id[file_id]["file_name"],
                        "score": decision["candidate_scores"].get(file_id),
                    }
                    for file_id in group["file_ids"]
                ],
            }
        )

    resume_value: Any = interrupt(
        {
            "kind": "file_governance_review",
            "instruction": "请为每个待审核版本组选择一个主版本文件 ID。",
            "groups": review_groups,
            "expected_schema": {
                "selections": {"<group_id>": "<file_id>"},
                "review_note": "可选说明",
            },
        }
    )
    if not isinstance(resume_value, dict):
        raise ValueError("人工审核恢复值必须是对象")
    selections = resume_value.get("selections")
    if not isinstance(selections, dict):
        raise ValueError("人工审核恢复值必须包含 selections 对象")
    if set(selections) != set(pending_ids):
        raise ValueError("selections 必须恰好覆盖全部待审核版本组")
    for group_id, selected_file_id in selections.items():
        if not isinstance(selected_file_id, str):
            raise ValueError(f"版本组 {group_id} 的选择必须是文件 ID 字符串")
        if selected_file_id not in group_by_id[group_id]["file_ids"]:
            raise ValueError(f"版本组 {group_id} 选择了组外文件")

    review_note = resume_value.get("review_note")
    if review_note is not None and not isinstance(review_note, str):
        raise ValueError("review_note 必须是字符串或 null")
    return {
        "human_review": {
            "pending_group_ids": pending_ids,
            "selections": dict(selections),
            "review_note": review_note,
        }
    }


def apply_human_selection(state: FileGovernanceState) -> dict:
    """把已校验的用户选择应用到对应推荐记录，并恢复运行状态。

    该节点只更新状态，不删除、移动、重命名或覆盖任何原始业务文件。启用
    Memory 时只记录版本组 ID 和所选文件 ID；用户自由文本说明不会进入数据库。

    Args:
        state: 已从 interrupt 恢复且人工选择通过成员关系校验的顶层状态。

    Returns:
        已应用人工选择的推荐、审核、运行和安全 Memory 状态。
    """
    group_by_id = {item["id"]: item for item in state.get("version_groups", [])}
    selections = state["human_review"]["selections"]
    decisions = []
    for decision in state.get("decisions", []):
        selected_file_id = selections.get(decision["group_id"])
        if selected_file_id is None:
            decisions.append(decision)
            continue
        decisions.append(
            apply_human_selection_service(
                decision,
                group_by_id[decision["group_id"]],
                selected_file_id,
            )
        )

    run = dict(state["run"])
    run.update({"status": "running", "current_stage": "human_review_applied"})
    memory = capture_human_choice_memory(
        state.get("memory"),
        source_run_id=run["run_id"],
        version_groups=state.get("version_groups", []),
        selections=selections,
    )
    return {
        "decisions": decisions,
        "human_review": {
            "pending_group_ids": [],
            "selections": dict(selections),
            "review_note": state["human_review"].get("review_note"),
        },
        "memory": memory,
        "run": run,
    }
