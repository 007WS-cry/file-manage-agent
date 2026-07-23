from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.nodes.memory import apply_recalled_memory, capture_recommendation_memory
from app.nodes.recommendation import (
    apply_branch_rules,
    apply_delivery_rules,
    apply_pdf_source_rules,
    calculate_decision_confidence,
    explain_recommendations,
    find_editable_leaf_versions,
    mark_human_review_items,
    preserve_complete_version_chains,
    score_version_candidates,
    select_main_versions,
    validate_recommendation_results,
)
from app.state.models import RecommendationGraphState

"""本模块构建按确定性规则顺序执行的独立 Recommendation 子图。"""


def build_recommendation_graph():
    """构建主版本候选评分、证据加权、置信度与审核标记子图。

    每个阶段顺序更新同一批 ``DecisionRecord``。置信度与保留策略不能并行
    覆盖同一 reducer 记录，因此先完成置信度计算，再写入完整版本保留清单。

    Returns:
        已编译、可独立调用且不带 Checkpointer 的 Recommendation LangGraph。
    """
    builder = StateGraph(RecommendationGraphState)
    builder.add_node("find_editable_leaf_versions", find_editable_leaf_versions)
    builder.add_node("score_version_candidates", score_version_candidates)
    builder.add_node("apply_recalled_memory", apply_recalled_memory)
    builder.add_node("apply_delivery_rules", apply_delivery_rules)
    builder.add_node("apply_pdf_source_rules", apply_pdf_source_rules)
    builder.add_node("apply_branch_rules", apply_branch_rules)
    builder.add_node("select_main_versions", select_main_versions)
    builder.add_node("explain_recommendations", explain_recommendations)
    builder.add_node("calculate_decision_confidence", calculate_decision_confidence)
    builder.add_node("preserve_complete_version_chains", preserve_complete_version_chains)
    builder.add_node("mark_human_review_items", mark_human_review_items)
    builder.add_node("validate_recommendation_results", validate_recommendation_results)
    builder.add_node(
        "capture_recommendation_memory",
        capture_recommendation_memory,
    )

    builder.add_edge(START, "find_editable_leaf_versions")
    builder.add_edge("find_editable_leaf_versions", "score_version_candidates")
    builder.add_edge("score_version_candidates", "apply_recalled_memory")
    builder.add_edge("apply_recalled_memory", "apply_delivery_rules")
    builder.add_edge("apply_delivery_rules", "apply_pdf_source_rules")
    builder.add_edge("apply_pdf_source_rules", "apply_branch_rules")
    builder.add_edge("apply_branch_rules", "select_main_versions")
    builder.add_edge("select_main_versions", "explain_recommendations")
    builder.add_edge("explain_recommendations", "calculate_decision_confidence")
    builder.add_edge(
        "calculate_decision_confidence",
        "preserve_complete_version_chains",
    )
    builder.add_edge("preserve_complete_version_chains", "mark_human_review_items")
    builder.add_edge("mark_human_review_items", "validate_recommendation_results")
    builder.add_edge(
        "validate_recommendation_results",
        "capture_recommendation_memory",
    )
    builder.add_edge("capture_recommendation_memory", END)
    return builder.compile()


# 已编译的独立 Recommendation 子图，包含历史偏好应用和短期 Memory 捕获。
recommendation_graph = build_recommendation_graph()
