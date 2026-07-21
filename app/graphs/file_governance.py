from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.graphs.routers import (
    has_analyzable_documents,
    has_pending_human_review,
    is_request_valid,
    route_after_run_hooks_result,
    route_before_run_hooks_result,
    route_evidence_result,
    route_failure_report_task_sync,
    route_system_prompt_result,
    route_team_orchestration_result,
    route_version_analysis_result,
)
from app.nodes.lifecycle import (
    execute_after_run_hooks,
    execute_before_run_hooks,
    finalize_run,
    initialize_run,
    load_system_prompt,
    validate_request,
)
from app.nodes.report import (
    generate_failure_report,
    generate_governance_report,
    generate_lifecycle_failure_report,
    generate_no_data_report,
)
from app.nodes.review import (
    apply_human_selection,
    prepare_human_review,
    request_human_review,
)
from app.nodes.subgraphs_nodes import (
    run_evidence_subgraph,
    run_inventory_subgraph,
    run_recommendation_subgraph,
    run_version_analysis_subgraph,
)
from app.nodes.task_tracking import (
    plan_run_tasks,
    sync_evidence_task_status,
    sync_human_review_task_status,
    sync_inventory_task_status,
    sync_recommendation_task_status,
    sync_report_task_status,
    sync_version_task_status,
)
from app.state.models import FileGovernanceState
from app.storage.checkpoints import create_memory_checkpointer

"""本模块构建接入 Task 进度、生命周期、四个业务子图和人工暂停恢复的顶层治理图。"""


def build_file_governance_graph(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """构建支持确定性 Task、Prompt、生命周期 Hook 和人工恢复的顶层治理图。

    Args:
        checkpointer: 可选 LangGraph Checkpointer。未提供时使用进程内
            ``InMemorySaver``，便于本地运行；生产环境应注入持久化实现。

    Returns:
        已编译、可使用 ``thread_id`` 调用和通过 ``Command(resume=...)``
        恢复人工审核的 LangGraph。
    """
    builder = StateGraph(FileGovernanceState)
    builder.add_node("initialize_run", initialize_run)
    builder.add_node("execute_before_run_hooks", execute_before_run_hooks)
    builder.add_node("validate_request", validate_request)
    builder.add_node("load_system_prompt", load_system_prompt)
    builder.add_node("plan_run_tasks", plan_run_tasks)
    builder.add_node("run_inventory_subgraph", run_inventory_subgraph)
    builder.add_node("sync_inventory_task_status", sync_inventory_task_status)
    builder.add_node("run_version_analysis_subgraph", run_version_analysis_subgraph)
    builder.add_node("sync_version_task_status", sync_version_task_status)
    builder.add_node("run_evidence_subgraph", run_evidence_subgraph)
    builder.add_node("sync_evidence_task_status", sync_evidence_task_status)
    builder.add_node("run_recommendation_subgraph", run_recommendation_subgraph)
    builder.add_node("sync_recommendation_task_status", sync_recommendation_task_status)
    builder.add_node("prepare_human_review", prepare_human_review)
    builder.add_node("request_human_review", request_human_review)
    builder.add_node("apply_human_selection", apply_human_selection)
    builder.add_node("sync_human_review_task_status", sync_human_review_task_status)
    builder.add_node("generate_failure_report", generate_failure_report)
    builder.add_node("generate_no_data_report", generate_no_data_report)
    builder.add_node("generate_governance_report", generate_governance_report)
    builder.add_node("sync_report_task_status", sync_report_task_status)
    builder.add_node("execute_after_run_hooks", execute_after_run_hooks)
    builder.add_node("generate_lifecycle_failure_report", generate_lifecycle_failure_report)
    builder.add_node("finalize_run", finalize_run)

    builder.add_edge(START, "initialize_run")
    builder.add_edge("initialize_run", "execute_before_run_hooks")
    builder.add_conditional_edges(
        "execute_before_run_hooks",
        route_before_run_hooks_result,
        {
            "continue": "validate_request",
            "failure": "generate_failure_report",
        },
    )
    builder.add_conditional_edges(
        "validate_request",
        is_request_valid,
        {"valid": "load_system_prompt", "invalid": "generate_failure_report"},
    )
    builder.add_conditional_edges(
        "load_system_prompt",
        route_system_prompt_result,
        {
            "continue": "plan_run_tasks",
            "failure": "generate_failure_report",
        },
    )
    builder.add_conditional_edges(
        "plan_run_tasks",
        route_team_orchestration_result,
        {
            "success": "run_inventory_subgraph",
            "failure": "generate_failure_report",
        },
    )
    builder.add_edge("run_inventory_subgraph", "sync_inventory_task_status")
    builder.add_conditional_edges(
        "sync_inventory_task_status",
        has_analyzable_documents,
        {
            "analyzable": "run_version_analysis_subgraph",
            "empty": "generate_no_data_report",
            "failure": "generate_failure_report",
        },
    )
    builder.add_edge("run_version_analysis_subgraph", "sync_version_task_status")
    builder.add_conditional_edges(
        "sync_version_task_status",
        route_version_analysis_result,
        {"success": "run_evidence_subgraph", "failure": "generate_failure_report"},
    )
    builder.add_edge("run_evidence_subgraph", "sync_evidence_task_status")
    builder.add_conditional_edges(
        "sync_evidence_task_status",
        route_evidence_result,
        {"success": "run_recommendation_subgraph", "failure": "generate_failure_report"},
    )
    builder.add_edge("run_recommendation_subgraph", "sync_recommendation_task_status")
    builder.add_conditional_edges(
        "sync_recommendation_task_status",
        has_pending_human_review,
        {
            "review": "prepare_human_review",
            "complete": "generate_governance_report",
            "failure": "generate_failure_report",
        },
    )
    builder.add_edge("prepare_human_review", "request_human_review")
    builder.add_edge("request_human_review", "apply_human_selection")
    builder.add_edge("apply_human_selection", "sync_human_review_task_status")
    builder.add_conditional_edges(
        "sync_human_review_task_status",
        route_team_orchestration_result,
        {
            "success": "generate_governance_report",
            "failure": "generate_failure_report",
        },
    )
    builder.add_conditional_edges(
        "generate_failure_report",
        route_failure_report_task_sync,
        {
            "sync": "sync_report_task_status",
            "skip": "execute_after_run_hooks",
        },
    )
    builder.add_edge("generate_no_data_report", "sync_report_task_status")
    builder.add_edge("generate_governance_report", "sync_report_task_status")
    builder.add_edge("sync_report_task_status", "execute_after_run_hooks")
    builder.add_conditional_edges(
        "execute_after_run_hooks",
        route_after_run_hooks_result,
        {
            "finalize": "finalize_run",
            "failure": "generate_lifecycle_failure_report",
        },
    )
    builder.add_edge("generate_lifecycle_failure_report", "finalize_run")
    builder.add_edge("finalize_run", END)
    selected_checkpointer = (
        checkpointer if checkpointer is not None else create_memory_checkpointer()
    )
    return builder.compile(checkpointer=selected_checkpointer)


# 默认使用进程内 Checkpointer 的已编译顶层治理图。
file_governance_graph = build_file_governance_graph()
