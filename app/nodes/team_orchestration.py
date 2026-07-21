from __future__ import annotations

from collections.abc import Sequence
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
from app.state.models import ErrorRecord, TaskItem, TeamOrchestrationGraphState
from app.utils.runtime import create_error_record, utc_now_iso

"""本模块实现 Team Orchestration 子图的 Task 规划、校验、更新和 Todo 投影节点。"""

# Task 状态允许的确定性转换；终态只能幂等保持，不能重新打开。
ALLOWED_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed", "skipped"}),
    "running": frozenset({"running", "completed", "failed", "skipped"}),
    "completed": frozenset({"completed"}),
    "failed": frozenset({"failed"}),
    "skipped": frozenset({"skipped"}),
}


def _orchestration_error(node_name: str, error: Exception) -> ErrorRecord:
    """把 Team Orchestration 节点异常转换为结构化致命校验错误。

    Args:
        node_name: 产生异常的节点函数名称。
        error: 已捕获且不会继续向 LangGraph 外传播的异常。

    Returns:
        可由顶层和子图 ``errors`` reducer 合并的结构化错误。
    """
    return create_error_record(
        stage="team_orchestration",
        node_name=node_name,
        category="validation",
        message=str(error),
        fatal=True,
    )


def _resolve_task_creation_time(state: TeamOrchestrationGraphState) -> str:
    """为首次创建或补齐 Task 选择稳定时间。

    优先使用顶层运行开始时间；旧 checkpoint 缺少开始时间时复用已有 Task 的
    ``created_at``，只有两者都不存在时才读取当前 UTC 时间。

    Args:
        state: 当前 Team Orchestration 子图状态。

    Returns:
        新建 Task 使用的 ISO 8601 时间字符串。
    """
    started_at = state.get("run", {}).get("started_at")
    if isinstance(started_at, str) and started_at.strip():
        return started_at
    for task in state.get("tasks", []):
        created_at = task.get("created_at")
        if isinstance(created_at, str) and created_at.strip():
            return created_at
    return utc_now_iso()


def _merge_output_refs(
    old_refs: Sequence[str],
    new_refs: Sequence[str],
) -> list[str]:
    """按首次出现顺序合并 Task 产物引用并拒绝空引用。

    Args:
        old_refs: Task 已经保存的产物引用。
        new_refs: 本次状态更新新返回的产物引用。

    Returns:
        去重且保持稳定顺序的产物引用列表。

    Raises:
        ValueError: 任意引用不是非空字符串时抛出。
    """
    merged: list[str] = []
    for reference in [*old_refs, *new_refs]:
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("Task output_refs 只能包含非空字符串引用")
        if reference not in merged:
            merged.append(reference)
    return merged


def _ensure_dependencies_ready(
    task: TaskItem,
    tasks_by_id: dict[str, TaskItem],
) -> None:
    """确认 Task 的所有依赖均已成功完成或正常跳过。

    Args:
        task: 等待进入运行或完成状态的目标 Task。
        tasks_by_id: 当前完整 Task DAG 的 ID 索引。

    Raises:
        ValueError: 依赖缺失、尚未完成、失败或因错误被阻断时抛出。
    """
    for dependency_id in task["dependencies"]:
        dependency = tasks_by_id.get(dependency_id)
        if dependency is None:
            raise ValueError(f"Task {task['task_id']} 引用了未知依赖：{dependency_id}")
        dependency_completed = dependency["status"] == "completed"
        dependency_skipped_normally = (
            dependency["status"] == "skipped" and not dependency.get("error")
        )
        if not dependency_completed and not dependency_skipped_normally:
            raise ValueError(
                f"Task {task['task_id']} 的依赖尚未就绪：{dependency_id}"
            )


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
            created_at=_resolve_task_creation_time(state),
            existing_tasks=state.get("tasks", []),
        )
        return {"tasks": tasks}
    except (KeyError, TypeError, ValueError) as error:
        return {"errors": [_orchestration_error("create_task_dag", error)]}


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
        return {"errors": [_orchestration_error("validate_task_dag", error)]}


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
        return {"errors": [_orchestration_error("assign_tasks_to_roles", error)]}


def update_task_status(state: TeamOrchestrationGraphState) -> dict:
    """消费一次私有 task_update 并确定性更新目标 Task。

    无更新命令时节点保持 Task 不变。终态 Task 只能幂等接收相同状态，不能重新
    打开；进入 running 或 completed 前必须确认全部依赖已完成或正常跳过。
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
            raise ValueError(
                f"Task {task_id} 不允许从 {old_status} 转换为 {new_status}"
            )

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
            _ensure_dependencies_ready(target, tasks_by_id)

        updated_task = dict(target)
        updated_task.update(
            {
                "status": new_status,
                "output_refs": _merge_output_refs(
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
            "errors": [_orchestration_error("update_task_status", error)],
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
        return {"errors": [_orchestration_error("update_todos_from_tasks", error)]}
