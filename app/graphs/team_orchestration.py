from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs.routers import route_task_dag_validation
from app.nodes.team_orchestration import (
    assign_tasks_to_roles,
    create_task_dag,
    update_task_status,
    update_todos_from_tasks,
    validate_task_dag,
)
from app.state.models import TeamOrchestrationGraphState

"""本模块构建并编译独立的确定性 Team Orchestration LangGraph 子图。"""


def build_team_orchestration_graph():
    """构建 Task 创建、校验、角色分配、状态更新和 Todo 投影子图。

    Returns:
        可独立调用且不带 Checkpointer、LLM、工具或 Subagent 节点的 LangGraph。
    """
    builder = StateGraph(TeamOrchestrationGraphState)
    builder.add_node("create_task_dag", create_task_dag)
    builder.add_node("validate_task_dag", validate_task_dag)
    builder.add_node("assign_tasks_to_roles", assign_tasks_to_roles)
    builder.add_node("update_task_status", update_task_status)
    builder.add_node("update_todos_from_tasks", update_todos_from_tasks)

    builder.add_edge(START, "create_task_dag")
    builder.add_edge("create_task_dag", "validate_task_dag")
    builder.add_conditional_edges(
        "validate_task_dag",
        route_task_dag_validation,
        {"valid": "assign_tasks_to_roles", "invalid": END},
    )
    builder.add_edge("assign_tasks_to_roles", "update_task_status")
    builder.add_edge("update_task_status", "update_todos_from_tasks")
    builder.add_edge("update_todos_from_tasks", END)
    return builder.compile()


# 已编译的 Team Orchestration 子图，供独立测试和顶层包装节点同步调用。
team_orchestration_graph = build_team_orchestration_graph()
