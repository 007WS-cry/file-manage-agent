from __future__ import annotations

from app.services.recommendation import (
    apply_branch_rules as apply_branch_rules_service,
)
from app.services.recommendation import (
    apply_delivery_rules as apply_delivery_rules_service,
)
from app.services.recommendation import (
    apply_pdf_source_rules as apply_pdf_source_rules_service,
)
from app.services.recommendation import (
    calculate_decision_confidence as calculate_decision_confidence_service,
)
from app.services.recommendation import create_scored_decision
from app.services.recommendation import (
    explain_recommendation as explain_recommendation_service,
)
from app.services.recommendation import (
    find_editable_leaf_versions as find_editable_leaf_versions_service,
)
from app.services.recommendation import (
    preserve_complete_version_chain as preserve_complete_version_chain_service,
)
from app.services.recommendation import (
    select_recommended_file as select_recommended_file_service,
)
from app.state.models import DecisionRecord, RecommendationCandidateSet, RecommendationGraphState
from app.utils.error_context import create_node_error

"""本模块实现独立 Recommendation 子图的候选、证据、选择与校验节点。"""


def find_editable_leaf_versions(state: RecommendationGraphState) -> dict:
    """为每个版本组收集非重复候选并标记可编辑叶子版本。

    Args:
        state: 已具有文件、版本组和版本链的 Recommendation 子图状态。

    Returns:
        每个版本组的候选集合，以及状态引用错误形成的致命错误。
    """
    chain_by_group = {
        item["group_id"]: item for item in state.get("version_chains", [])
    }
    candidate_sets: list[RecommendationCandidateSet] = []
    errors = []
    for group in state.get("version_groups", []):
        try:
            chain = chain_by_group[group["id"]]
            candidate_sets.append(
                find_editable_leaf_versions_service(
                    group,
                    state.get("files", []),
                    chain,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(
                create_node_error(
                    state,
                    stage="recommendation",
                    node_name="find_editable_leaf_versions",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            )
    return {"candidate_sets": candidate_sets, "errors": errors}


def score_version_candidates(state: RecommendationGraphState) -> dict:
    """根据版本链、可编辑性、时间与文件名为组内候选建立基础分。

    Args:
        state: 已建立 Recommendation 候选集合的子图状态。

    Returns:
        每个版本组的基础推荐记录，以及无法评分时的致命错误。
    """
    chain_by_group = {
        item["group_id"]: item for item in state.get("version_chains", [])
    }
    candidate_by_group = {
        item["group_id"]: item for item in state.get("candidate_sets", [])
    }
    decisions: list[DecisionRecord] = []
    errors = []
    for group in state.get("version_groups", []):
        try:
            decisions.append(
                create_scored_decision(
                    group,
                    state.get("files", []),
                    chain_by_group[group["id"]],
                    candidate_by_group[group["id"]],
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(
                create_node_error(
                    state,
                    stage="recommendation",
                    node_name="score_version_candidates",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            )
    return {"decisions": decisions, "errors": errors}


def apply_delivery_rules(state: RecommendationGraphState) -> dict:
    """把已匹配发送记录和客户确认信号应用到候选评分。

    Args:
        state: 已完成基础候选评分并包含发送证据的子图状态。

    Returns:
        应用发送证据后的推荐记录，以及证据关系错误。
    """
    decisions: list[DecisionRecord] = []
    errors = []
    for decision in state.get("decisions", []):
        try:
            decisions.append(
                apply_delivery_rules_service(
                    decision,
                    state.get("deliveries", []),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(
                create_node_error(
                    state,
                    stage="recommendation",
                    node_name="apply_delivery_rules",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            )
    return {"decisions": decisions, "errors": errors}


def apply_pdf_source_rules(state: RecommendationGraphState) -> dict:
    """用 PDF 来源关系提高可编辑源版本并降低导出件优先级。

    Args:
        state: 已应用发送证据并包含 PDF 来源记录的子图状态。

    Returns:
        应用 PDF 来源证据后的推荐记录，以及证据关系错误。
    """
    decisions: list[DecisionRecord] = []
    errors = []
    for decision in state.get("decisions", []):
        try:
            decisions.append(
                apply_pdf_source_rules_service(
                    decision,
                    state.get("pdf_exports", []),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(
                create_node_error(
                    state,
                    stage="recommendation",
                    node_name="apply_pdf_source_rules",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            )
    return {"decisions": decisions, "errors": errors}


def apply_branch_rules(state: RecommendationGraphState) -> dict:
    """把版本分叉加入推荐解释并保留所有分支供人工判断。

    Args:
        state: 已完成外部证据加权且包含分叉记录的子图状态。

    Returns:
        带分叉说明的推荐记录。
    """
    decisions = [
        apply_branch_rules_service(decision, state.get("branches", []))
        for decision in state.get("decisions", [])
    ]
    return {"decisions": decisions}


def select_main_versions(state: RecommendationGraphState) -> dict:
    """在每个版本组内部确定当前评分最高的主版本候选。

    Args:
        state: 已完成全部规则加权的 Recommendation 子图状态。

    Returns:
        写入当前推荐文件的推荐记录，以及候选引用错误。
    """
    decisions: list[DecisionRecord] = []
    errors = []
    for decision in state.get("decisions", []):
        try:
            decisions.append(
                select_recommended_file_service(
                    decision,
                    state.get("files", []),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(
                create_node_error(
                    state,
                    stage="recommendation",
                    node_name="select_main_versions",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            )
    return {"decisions": decisions, "errors": errors}


def explain_recommendations(state: RecommendationGraphState) -> dict:
    """为每个当前最高候选组合基础评分、证据和分叉解释。

    Args:
        state: 已选出当前主版本候选的 Recommendation 子图状态。

    Returns:
        带完整确定性解释的推荐记录，以及状态引用错误。
    """
    group_by_id = {item["id"]: item for item in state.get("version_groups", [])}
    chain_by_group = {
        item["group_id"]: item for item in state.get("version_chains", [])
    }
    decisions: list[DecisionRecord] = []
    errors = []
    for decision in state.get("decisions", []):
        try:
            group_id = decision["group_id"]
            decisions.append(
                explain_recommendation_service(
                    decision,
                    group_by_id[group_id],
                    state.get("files", []),
                    chain_by_group[group_id],
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(
                create_node_error(
                    state,
                    stage="recommendation",
                    node_name="explain_recommendations",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            )
    return {"decisions": decisions, "errors": errors}


def calculate_decision_confidence(state: RecommendationGraphState) -> dict:
    """根据候选分差、版本链和分叉计算最终推荐置信度。

    Args:
        state: 已具备候选选择和解释的 Recommendation 子图状态。

    Returns:
        写入置信度、选择来源和人工审核标记的推荐记录。
    """
    chain_by_group = {
        item["group_id"]: item for item in state.get("version_chains", [])
    }
    decisions: list[DecisionRecord] = []
    errors = []
    for decision in state.get("decisions", []):
        try:
            decisions.append(
                calculate_decision_confidence_service(
                    decision,
                    chain_by_group[decision["group_id"]],
                    state.get("branches", []),
                    auto_select_threshold=state["request"]["auto_select_threshold"],
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(
                create_node_error(
                    state,
                    stage="recommendation",
                    node_name="calculate_decision_confidence",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            )
    return {"decisions": decisions, "errors": errors}


def preserve_complete_version_chains(state: RecommendationGraphState) -> dict:
    """为每项推荐写入完整组内版本保留清单。

    推荐结果只表达主版本偏好，不构成删除、移动、重命名或覆盖授权；即使版本
    链不完整，也会保留该组全部成员并交由人工审核。

    Args:
        state: 已完成最终置信度计算的 Recommendation 子图状态。

    Returns:
        包含完整保留清单的推荐记录，以及版本链引用错误。
    """
    group_by_id = {item["id"]: item for item in state.get("version_groups", [])}
    chain_by_group = {
        item["group_id"]: item for item in state.get("version_chains", [])
    }
    decisions: list[DecisionRecord] = []
    errors = []
    for decision in state.get("decisions", []):
        try:
            group_id = decision["group_id"]
            decisions.append(
                preserve_complete_version_chain_service(
                    decision,
                    group_by_id[group_id],
                    chain_by_group[group_id],
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(
                create_node_error(
                    state,
                    stage="recommendation",
                    node_name="preserve_complete_version_chains",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            )
    return {"decisions": decisions, "errors": errors}


def mark_human_review_items(state: RecommendationGraphState) -> dict:
    """汇总所有未达到自动选择条件的版本组供顶层人工审核。

    Args:
        state: 已完成保留策略的 Recommendation 子图状态。

    Returns:
        待审核版本组 ID、空选择映射和保留的审核说明。
    """
    pending_group_ids = sorted(
        decision["group_id"]
        for decision in state.get("decisions", [])
        if decision["needs_human_review"]
    )
    previous_review = state.get(
        "human_review",
        {"pending_group_ids": [], "selections": {}, "review_note": None},
    )
    return {
        "human_review": {
            "pending_group_ids": pending_group_ids,
            "selections": {},
            "review_note": previous_review.get("review_note"),
        }
    }


def validate_recommendation_results(state: RecommendationGraphState) -> dict:
    """校验候选集合、推荐、保留清单和人工审核状态的一致性。

    Args:
        state: 已完成全部 Recommendation 规则节点的子图状态。

    Returns:
        状态一致时返回空更新，否则返回去重后的致命校验错误。
    """
    file_ids = {item["id"] for item in state.get("files", [])}
    group_by_id = {item["id"]: item for item in state.get("version_groups", [])}
    group_ids = set(group_by_id)
    candidate_by_group = {
        item["group_id"]: item for item in state.get("candidate_sets", [])
    }
    decision_by_group = {
        item["group_id"]: item for item in state.get("decisions", [])
    }
    messages = []

    if set(candidate_by_group) != group_ids:
        messages.append("候选集合与版本组未形成一一对应关系")
    if set(decision_by_group) != group_ids:
        messages.append("推荐结果与版本组未形成一一对应关系")

    for group_id, candidate_set in candidate_by_group.items():
        group = group_by_id.get(group_id)
        if group is None:
            messages.append(f"候选集合引用未知版本组 {group_id}")
            continue
        candidate_ids = set(candidate_set["candidate_file_ids"])
        editable_leaf_ids = set(candidate_set["editable_leaf_file_ids"])
        if not candidate_ids.issubset(group["file_ids"]):
            messages.append(f"版本组 {group_id} 的候选集合包含组外文件")
        if not editable_leaf_ids.issubset(candidate_ids):
            messages.append(f"版本组 {group_id} 的可编辑叶子不属于候选集合")
        if not candidate_ids.issubset(file_ids):
            messages.append(f"版本组 {group_id} 的候选集合引用未知文件")

    expected_review_ids = set()
    for group_id, decision in decision_by_group.items():
        group = group_by_id.get(group_id)
        if group is None:
            messages.append(f"推荐结果引用未知版本组 {group_id}")
            continue
        candidate_ids = set(decision["candidate_scores"])
        expected_candidate_ids = set(
            candidate_by_group.get(group_id, {}).get("candidate_file_ids", [])
        )
        if candidate_ids != expected_candidate_ids:
            messages.append(f"版本组 {group_id} 的候选评分与候选集合不一致")
        if any(not 0.0 <= score <= 1.0 for score in decision["candidate_scores"].values()):
            messages.append(f"版本组 {group_id} 包含非法候选评分")
        recommended_file_id = decision["recommended_file_id"]
        if candidate_ids and recommended_file_id not in candidate_ids:
            messages.append(f"版本组 {group_id} 的推荐文件不属于候选集合")
        if not candidate_ids and recommended_file_id is not None:
            messages.append(f"无候选版本组 {group_id} 不应包含推荐文件")
        if not 0.0 <= decision["confidence"] <= 1.0:
            messages.append(f"版本组 {group_id} 的推荐置信度非法")
        if set(decision["preserve_file_ids"]) != set(group["file_ids"]):
            messages.append(f"版本组 {group_id} 未完整保留全部版本文件")
        if decision["needs_human_review"]:
            expected_review_ids.add(group_id)
            if decision["selected_by"] != "unresolved":
                messages.append(f"待审核版本组 {group_id} 的选择来源必须为 unresolved")
        elif decision["selected_by"] != "rule":
            messages.append(f"自动推荐版本组 {group_id} 的选择来源必须为 rule")

    actual_review_ids = set(state["human_review"]["pending_group_ids"])
    if actual_review_ids != expected_review_ids:
        messages.append("人工审核版本组与推荐结果不一致")
    if state["human_review"]["selections"]:
        messages.append("独立 Recommendation 子图不应预填人工选择结果")

    if not messages:
        return {}
    return {
        "errors": [
            create_node_error(
                state,
                stage="recommendation",
                node_name="validate_recommendation_results",
                category="validation",
                message=message,
                fatal=True,
            )
            for message in dict.fromkeys(messages)
        ]
    }
