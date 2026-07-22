from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs.routers import (
    route_orchestration_action,
    route_subagent_payload_validation,
    route_task_dag_validation,
    route_team_initialization_result,
    route_team_message_validation,
    select_subagent,
)
from app.nodes.team_orchestration import (
    append_task_output_refs,
    assign_tasks_to_roles,
    build_fallback_result_message,
    create_assignment_message,
    create_task_dag,
    fallback_to_coordinator,
    initialize_fixed_agent_team,
    invoke_content_subagent_graph,
    invoke_evidence_subagent_graph,
    invoke_version_subagent_graph,
    merge_subagent_artifacts,
    update_task_status,
    update_todos_from_tasks,
    validate_orchestration_action,
    validate_subagent_payload,
    validate_task_dag,
    validate_team_message,
)
from app.state.models import TeamOrchestrationGraphState

"""本模块构建固定团队的 Task 同步、Subagent 分派和协调者回退编排子图。"""


def build_team_orchestration_graph():
    """构建 Task 同步及三个固定 Subagent 的统一分派编排子图。

    Returns:
        可独立调用、验证 Team Protocol 并在失败时确定性回退的 LangGraph。
    """
    builder = StateGraph(TeamOrchestrationGraphState)
    builder.add_node("create_task_dag", create_task_dag)
    builder.add_node("validate_task_dag", validate_task_dag)
    builder.add_node("initialize_fixed_agent_team", initialize_fixed_agent_team)
    builder.add_node("assign_tasks_to_roles", assign_tasks_to_roles)
    builder.add_node("validate_orchestration_action", validate_orchestration_action)
    builder.add_node("update_task_status", update_task_status)
    builder.add_node("update_todos_from_tasks", update_todos_from_tasks)
    builder.add_node("validate_subagent_payload", validate_subagent_payload)
    builder.add_node("create_assignment_message", create_assignment_message)
    builder.add_node("invoke_content_subagent_graph", invoke_content_subagent_graph)
    builder.add_node("invoke_version_subagent_graph", invoke_version_subagent_graph)
    builder.add_node("invoke_evidence_subagent_graph", invoke_evidence_subagent_graph)
    builder.add_node("validate_team_message", validate_team_message)
    builder.add_node("fallback_to_coordinator", fallback_to_coordinator)
    builder.add_node("build_fallback_result_message", build_fallback_result_message)
    builder.add_node("merge_subagent_artifacts", merge_subagent_artifacts)
    builder.add_node("append_task_output_refs", append_task_output_refs)

    builder.add_edge(START, "create_task_dag")
    builder.add_edge("create_task_dag", "validate_task_dag")
    builder.add_conditional_edges(
        "validate_task_dag",
        route_task_dag_validation,
        {"valid": "initialize_fixed_agent_team", "invalid": END},
    )
    builder.add_conditional_edges(
        "initialize_fixed_agent_team",
        route_team_initialization_result,
        {"valid": "assign_tasks_to_roles", "invalid": END},
    )
    builder.add_edge("assign_tasks_to_roles", "validate_orchestration_action")
    builder.add_conditional_edges(
        "validate_orchestration_action",
        route_orchestration_action,
        {
            "status_sync": "update_task_status",
            "dispatch": "validate_subagent_payload",
            "invalid": END,
        },
    )
    builder.add_conditional_edges(
        "validate_subagent_payload",
        route_subagent_payload_validation,
        {
            "assign": "create_assignment_message",
            "fallback": "fallback_to_coordinator",
        },
    )
    builder.add_conditional_edges(
        "create_assignment_message",
        select_subagent,
        {
            "content": "invoke_content_subagent_graph",
            "version": "invoke_version_subagent_graph",
            "evidence": "invoke_evidence_subagent_graph",
            "fallback": "fallback_to_coordinator",
        },
    )
    builder.add_edge("invoke_content_subagent_graph", "validate_team_message")
    builder.add_edge("invoke_version_subagent_graph", "validate_team_message")
    builder.add_edge("invoke_evidence_subagent_graph", "validate_team_message")
    builder.add_conditional_edges(
        "validate_team_message",
        route_team_message_validation,
        {
            "merge": "merge_subagent_artifacts",
            "fallback": "fallback_to_coordinator",
        },
    )
    builder.add_edge("fallback_to_coordinator", "build_fallback_result_message")
    builder.add_edge("build_fallback_result_message", "merge_subagent_artifacts")
    builder.add_edge("merge_subagent_artifacts", "append_task_output_refs")
    builder.add_edge("append_task_output_refs", "update_todos_from_tasks")
    builder.add_edge("update_task_status", "update_todos_from_tasks")
    builder.add_edge("update_todos_from_tasks", END)
    return builder.compile()


# 已编译的 Team Orchestration 子图，支持 Task 同步和单个固定 Subagent 分派。
team_orchestration_graph = build_team_orchestration_graph()
