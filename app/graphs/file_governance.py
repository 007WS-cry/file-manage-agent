from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.graphs.routers import (
    has_analyzable_documents,
    has_pending_human_review,
    is_request_valid,
)
from app.nodes.lifecycle import finalize_run, initialize_run, validate_request
from app.nodes.report import (
    generate_failure_report,
    generate_governance_report,
    generate_no_data_report,
)
from app.nodes.review import (
    apply_human_selection,
    prepare_human_review,
    request_human_review,
)
from app.nodes.subgraphs_nodes import run_inventory_subgraph, run_version_analysis_subgraph
from app.state.models import FileGovernanceState
from app.storage.checkpoints import create_memory_checkpointer

"""本模块构建顶层文件治理图，并直接接入 Inventory 与 Version Analysis 子图。"""


def build_file_governance_graph(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """构建支持人工暂停恢复的顶层文件版本治理图。

    Args:
        checkpointer: 可选 LangGraph Checkpointer。未提供时使用进程内
            ``InMemorySaver``，便于本地运行；生产环境应注入持久化实现。

    Returns:
        已编译、可使用 ``thread_id`` 调用和通过 ``Command(resume=...)``
        恢复人工审核的 LangGraph。
    """
    builder = StateGraph(FileGovernanceState)
    builder.add_node("initialize_run", initialize_run)
    builder.add_node("validate_request", validate_request)
    builder.add_node("run_inventory_subgraph", run_inventory_subgraph)
    builder.add_node("run_version_analysis_subgraph", run_version_analysis_subgraph)
    builder.add_node("prepare_human_review", prepare_human_review)
    builder.add_node("request_human_review", request_human_review)
    builder.add_node("apply_human_selection", apply_human_selection)
    builder.add_node("generate_failure_report", generate_failure_report)
    builder.add_node("generate_no_data_report", generate_no_data_report)
    builder.add_node("generate_governance_report", generate_governance_report)
    builder.add_node("finalize_run", finalize_run)

    builder.add_edge(START, "initialize_run")
    builder.add_edge("initialize_run", "validate_request")
    builder.add_conditional_edges(
        "validate_request",
        is_request_valid,
        {"valid": "run_inventory_subgraph", "invalid": "generate_failure_report"},
    )
    builder.add_conditional_edges(
        "run_inventory_subgraph",
        has_analyzable_documents,
        {
            "analyzable": "run_version_analysis_subgraph",
            "empty": "generate_no_data_report",
            "failure": "generate_failure_report",
        },
    )
    builder.add_conditional_edges(
        "run_version_analysis_subgraph",
        has_pending_human_review,
        {
            "review": "prepare_human_review",
            "complete": "generate_governance_report",
            "failure": "generate_failure_report",
        },
    )
    builder.add_edge("prepare_human_review", "request_human_review")
    builder.add_edge("request_human_review", "apply_human_selection")
    builder.add_edge("apply_human_selection", "generate_governance_report")
    builder.add_edge("generate_failure_report", "finalize_run")
    builder.add_edge("generate_no_data_report", "finalize_run")
    builder.add_edge("generate_governance_report", "finalize_run")
    builder.add_edge("finalize_run", END)
    selected_checkpointer = (
        checkpointer if checkpointer is not None else create_memory_checkpointer()
    )
    return builder.compile(checkpointer=selected_checkpointer)


# 默认使用进程内 Checkpointer 的已编译顶层治理图。
file_governance_graph = build_file_governance_graph()
