from __future__ import annotations

from typing import cast

from app.services.task_system import (
    assign_tasks_to_roles as assign_roles,
)
from app.services.task_system import (
    create_task_dag as create_fixed_task_dag,
)
from app.services.task_system import (
    update_todos_from_tasks as project_todos_from_tasks,
)
from app.services.task_system import (
    validate_task_dag as validate_fixed_task_dag,
)
from app.state.models import TaskItem, TeamOrchestrationGraphState
from app.utils.task_orchestration import (
    ALLOWED_TASK_TRANSITIONS,
    create_orchestration_error,
    ensure_task_dependencies_ready,
    merge_task_output_refs,
    resolve_task_creation_time,
)

"""本模块只定义 Team Orchestration 子图的规划、校验、更新和 Todo 投影节点。"""


def create_task_dag(state: TeamOrchestrationGraphState) -> dict:
    """幂等创建或补齐当前运行的固定 Task DAG。

    Args:
        state: 包含运行信息和可选已有 Task 的团队编排状态。

    Returns:
        完整 Task 列表；参数或已有 DAG 非法时返回结构化致命错误。
    """
    try:
        tasks = create_fixed_task_dag(
            state["run"]["run_id"],
            created_at=resolve_task_creation_time(state),
            existing_tasks=state.get("tasks", []),
        )
        return {"tasks": tasks}
    except (KeyError, TypeError, ValueError) as error:
        return {"errors": [create_orchestration_error("create_task_dag", error)]}


def validate_task_dag(state: TeamOrchestrationGraphState) -> dict:
    """验证子图状态中的 Task 是否构成合法 DAG。

    Args:
        state: 已经过 Task 创建节点的团队编排状态。

    Returns:
        校验成功时返回空更新；失败时返回结构化致命错误。
    """
    try:
        validate_fixed_task_dag(state.get("tasks", []))
        return {}
    except (KeyError, TypeError, ValueError) as error:
        return {"errors": [create_orchestration_error("validate_task_dag", error)]}


def assign_tasks_to_roles(state: TeamOrchestrationGraphState) -> dict:
    """为合法 Task DAG 写入固定逻辑角色，不调用真实 Agent。

    Args:
        state: 已通过 DAG 校验的团队编排状态。

    Returns:
        角色已校正的 Task 列表；无法分配时返回结构化致命错误。
    """
    try:
        return {"tasks": assign_roles(state.get("tasks", []))}
    except (KeyError, TypeError, ValueError) as error:
        return {"errors": [create_orchestration_error("assign_tasks_to_roles", error)]}


def update_task_status(state: TeamOrchestrationGraphState) -> dict:
    """消费一次私有 task_update 并确定性更新目标 Task。

    无更新命令时节点保持 Task 不变。终态 Task 只能幂等接收相同状态，不能重新
    打开；普通 Task 进入 running 或 completed 前必须确认依赖成功终结，Report Task
    则可在直接依赖进入任一终态后生成成功、无数据或失败报告。
    无论更新成功还是失败，命令都会被清空，防止直接重放子图时重复应用。

    Args:
        state: 包含完整 Task DAG 和可选单次更新命令的团队编排状态。

    Returns:
        Task 局部更新和已清空的 task_update；非法转换同时返回结构化致命错误。
    """
    task_update = state.get("task_update")
    if task_update is None:
        return {"task_update": None}

    try:
        tasks = state.get("tasks", [])
        validate_fixed_task_dag(tasks)
        tasks_by_id = {task["task_id"]: task for task in tasks}
        task_id = task_update["task_id"]
        target = tasks_by_id.get(task_id)
        if target is None:
            raise ValueError(f"task_update 引用了未知 Task：{task_id}")

        new_status = task_update["status"]
        old_status = target["status"]
        allowed = ALLOWED_TASK_TRANSITIONS.get(old_status, frozenset())
        if new_status not in allowed:
            raise ValueError(f"Task {task_id} 不允许从 {old_status} 转换为 {new_status}")

        if old_status in {"completed", "failed", "skipped"}:
            return {"task_update": None}

        updated_at = task_update["updated_at"]
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise ValueError("TaskStatusUpdate.updated_at 必须是非空时间字符串")
        error_message = task_update.get("error")
        if new_status == "failed" and not error_message:
            raise ValueError("Task 进入 failed 状态时必须提供 error")
        if new_status in {"running", "completed"} and error_message:
            raise ValueError(f"Task 进入 {new_status} 状态时 error 必须为 None")
        if new_status in {"running", "completed"}:
            ensure_task_dependencies_ready(target, tasks_by_id)

        updated_task = dict(target)
        updated_task.update(
            {
                "status": new_status,
                "output_refs": merge_task_output_refs(
                    target.get("output_refs", []),
                    task_update.get("output_refs", []),
                ),
                "error": error_message,
                "updated_at": updated_at,
            }
        )
        return {
            "tasks": [cast(TaskItem, updated_task)],
            "task_update": None,
        }
    except (KeyError, TypeError, ValueError) as error:
        return {
            "task_update": None,
            "errors": [create_orchestration_error("update_task_status", error)],
        }


def update_todos_from_tasks(state: TeamOrchestrationGraphState) -> dict:
    """仅根据最新完整 Task DAG 重新生成 Todo 用户视图。

    Args:
        state: 已完成可选 Task 状态更新的团队编排状态。

    Returns:
        全量 Todo 投影；Task 非法时返回结构化致命错误。
    """
    try:
        todos = project_todos_from_tasks(
            state["run"]["run_id"],
            state.get("tasks", []),
        )
        return {"todos": todos}
    except (KeyError, TypeError, ValueError) as error:
        return {"errors": [create_orchestration_error("update_todos_from_tasks", error)]}
