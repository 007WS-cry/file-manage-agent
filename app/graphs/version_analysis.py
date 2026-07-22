from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs.routers import (
    comparison_succeeded,
    has_pending_comparisons,
    has_valid_version_subagent_summary,
)
from app.nodes.version_analysis import (
    add_duplicate_version_edges,
    apply_subagent_summary,
    build_comparison_queue,
    build_version_chains,
    build_version_edges,
    compare_document_pair,
    detect_version_branches,
    generate_candidate_pairs,
    group_related_documents,
    infer_version_direction,
    load_next_comparison,
    prepare_version_subagent_input,
    record_comparison_error,
    record_diff_result,
    retain_deterministic_summary,
    summarize_key_changes_deterministically,
    summarize_key_changes_with_subagent,
    validate_version_results,
)
from app.state.models import VersionAnalysisGraphState

"""本模块构建版本分组、确定性比较、Version 摘要升级和版本建链子图。"""


def build_version_analysis_graph():
    """构建版本分析子图并确保比较循环与版本建链节点完整连通。

    Returns:
        已编译、可由顶层图同步调用且不包含推荐节点的 Version Analysis 子图。
    """
    builder = StateGraph(VersionAnalysisGraphState)
    builder.add_node("group_related_documents", group_related_documents)
    builder.add_node("add_duplicate_version_edges", add_duplicate_version_edges)
    builder.add_node("generate_candidate_pairs", generate_candidate_pairs)
    builder.add_node("build_comparison_queue", build_comparison_queue)
    builder.add_node("load_next_comparison", load_next_comparison)
    builder.add_node("compare_document_pair", compare_document_pair)
    builder.add_node("infer_version_direction", infer_version_direction)
    builder.add_node(
        "summarize_key_changes_deterministically",
        summarize_key_changes_deterministically,
    )
    builder.add_node("prepare_version_subagent_input", prepare_version_subagent_input)
    builder.add_node(
        "summarize_key_changes_with_subagent",
        summarize_key_changes_with_subagent,
    )
    builder.add_node("apply_subagent_summary", apply_subagent_summary)
    builder.add_node("retain_deterministic_summary", retain_deterministic_summary)
    builder.add_node("record_diff_result", record_diff_result)
    builder.add_node("record_comparison_error", record_comparison_error)
    builder.add_node("build_version_edges", build_version_edges)
    builder.add_node("detect_version_branches", detect_version_branches)
    builder.add_node("build_version_chains", build_version_chains)
    builder.add_node("validate_version_results", validate_version_results)

    builder.add_edge(START, "group_related_documents")
    builder.add_edge("group_related_documents", "add_duplicate_version_edges")
    builder.add_edge("add_duplicate_version_edges", "generate_candidate_pairs")
    builder.add_edge("generate_candidate_pairs", "build_comparison_queue")
    builder.add_conditional_edges(
        "build_comparison_queue",
        has_pending_comparisons,
        {"pending": "load_next_comparison", "done": "build_version_edges"},
    )
    builder.add_edge("load_next_comparison", "compare_document_pair")
    builder.add_edge("compare_document_pair", "infer_version_direction")
    builder.add_edge("infer_version_direction", "summarize_key_changes_deterministically")
    builder.add_edge(
        "summarize_key_changes_deterministically",
        "prepare_version_subagent_input",
    )
    builder.add_edge(
        "prepare_version_subagent_input",
        "summarize_key_changes_with_subagent",
    )
    builder.add_conditional_edges(
        "summarize_key_changes_with_subagent",
        has_valid_version_subagent_summary,
        {
            "apply": "apply_subagent_summary",
            "deterministic": "retain_deterministic_summary",
            "comparison_failure": "record_comparison_error",
        },
    )
    builder.add_conditional_edges(
        "apply_subagent_summary",
        comparison_succeeded,
        {"success": "record_diff_result", "failure": "record_comparison_error"},
    )
    builder.add_conditional_edges(
        "retain_deterministic_summary",
        comparison_succeeded,
        {"success": "record_diff_result", "failure": "record_comparison_error"},
    )
    builder.add_conditional_edges(
        "record_diff_result",
        has_pending_comparisons,
        {"pending": "load_next_comparison", "done": "build_version_edges"},
    )
    builder.add_conditional_edges(
        "record_comparison_error",
        has_pending_comparisons,
        {"pending": "load_next_comparison", "done": "build_version_edges"},
    )
    builder.add_edge("build_version_edges", "detect_version_branches")
    builder.add_edge("detect_version_branches", "build_version_chains")
    builder.add_edge("build_version_chains", "validate_version_results")
    builder.add_edge("validate_version_results", END)
    return builder.compile()


# 已编译的版本分析子图，供顶层治理图直接作为子图节点接入。
version_analysis_graph = build_version_analysis_graph()
