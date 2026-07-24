from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.graphs.routers import (
    route_recovery_action,
    route_recovery_human_action,
    route_recovery_reuse_result,
)
from app.nodes.error_recovery import (
    apply_recovery_fallback,
    apply_recovery_human_input,
    decide_recovery_action,
    finalize_recovery_outcome,
    inspect_reusable_execution,
    mark_recovery_aborted,
    prepare_recovery_human_input,
    request_recovery_human_input,
    schedule_recovery_retry,
    select_recovery_error,
)
from app.state.models import RecoveryGraphState

"""本模块构建结果复用、有限重试、安全降级、人工恢复和终止判断组成的第七个子图。"""


def build_error_recovery_graph(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """构建不直接执行业务节点的 Error Recovery 子图。

    子图只产生固定恢复动作和状态更新；失败节点重试及正常后继选择由顶层图的
    ``resume_failed_stage`` 与 ``resume_after_failed_stage`` 条件路由执行。

    Args:
        checkpointer: 可选独立 Checkpointer；顶层嵌套调用时保持为 None。

    Returns:
        已编译、可由顶层包装节点同步调用的 Error Recovery LangGraph。
    """
    builder = StateGraph(RecoveryGraphState)
    builder.add_node("select_recovery_error", select_recovery_error)
    builder.add_node("inspect_reusable_execution", inspect_reusable_execution)
    builder.add_node("decide_recovery_action", decide_recovery_action)
    builder.add_node("schedule_recovery_retry", schedule_recovery_retry)
    builder.add_node("apply_recovery_fallback", apply_recovery_fallback)
    builder.add_node(
        "prepare_recovery_human_input",
        prepare_recovery_human_input,
    )
    builder.add_node(
        "request_recovery_human_input",
        request_recovery_human_input,
    )
    builder.add_node(
        "apply_recovery_human_input",
        apply_recovery_human_input,
    )
    builder.add_node("mark_recovery_aborted", mark_recovery_aborted)
    builder.add_node("finalize_recovery_outcome", finalize_recovery_outcome)

    builder.add_edge(START, "select_recovery_error")
    builder.add_edge("select_recovery_error", "inspect_reusable_execution")
    builder.add_conditional_edges(
        "inspect_reusable_execution",
        route_recovery_reuse_result,
        {
            "reused": "finalize_recovery_outcome",
            "decide": "decide_recovery_action",
            "abort": "mark_recovery_aborted",
        },
    )
    builder.add_conditional_edges(
        "decide_recovery_action",
        route_recovery_action,
        {
            "retry": "schedule_recovery_retry",
            "fallback": "apply_recovery_fallback",
            "wait_human": "prepare_recovery_human_input",
            "abort": "mark_recovery_aborted",
        },
    )
    builder.add_edge(
        "prepare_recovery_human_input",
        "request_recovery_human_input",
    )
    builder.add_edge(
        "request_recovery_human_input",
        "apply_recovery_human_input",
    )
    builder.add_conditional_edges(
        "apply_recovery_human_input",
        route_recovery_human_action,
        {
            "retry": "schedule_recovery_retry",
            "fallback": "apply_recovery_fallback",
            "abort": "mark_recovery_aborted",
        },
    )
    builder.add_edge("schedule_recovery_retry", "finalize_recovery_outcome")
    builder.add_edge("apply_recovery_fallback", "finalize_recovery_outcome")
    builder.add_edge("mark_recovery_aborted", "finalize_recovery_outcome")
    builder.add_edge("finalize_recovery_outcome", END)
    return builder.compile(checkpointer=checkpointer)


# 默认已编译的第七个 Error Recovery 子图。
error_recovery_graph = build_error_recovery_graph()
