from __future__ import annotations

from collections.abc import Sequence

from app.state.models import ErrorRecord, TaskItem, TeamOrchestrationGraphState
from app.utils.runtime import create_error_record, utc_now_iso

"""本模块提供 Team Orchestration 节点使用的状态转换与错误收敛辅助能力。"""

# Task 状态允许的确定性转换；终态只能幂等保持，不能重新打开。
ALLOWED_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed", "skipped"}),
    "running": frozenset({"running", "completed", "failed", "skipped"}),
    "completed": frozenset({"completed"}),
    "failed": frozenset({"failed"}),
    "skipped": frozenset({"skipped"}),
}


def create_orchestration_error(node_name: str, error: Exception) -> ErrorRecord:
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


def resolve_task_creation_time(state: TeamOrchestrationGraphState) -> str:
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


def merge_task_output_refs(
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


def ensure_task_dependencies_ready(
    task: TaskItem,
    tasks_by_id: dict[str, TaskItem],
) -> None:
    """确认 Task 的所有依赖满足当前阶段的启动条件。

    普通业务 Task 只接受已完成或无错误跳过的依赖。报告 Task 是治理运行的统一
    收口阶段，因此允许依赖以 completed、failed 或 skipped 任一终态结束，确保
    失败报告仍可记录真实故障，而不会把报告自身误判为失败。

    Args:
        task: 等待进入运行或完成状态的目标 Task。
        tasks_by_id: 当前完整 Task DAG 的 ID 索引。

    Raises:
        ValueError: 依赖缺失或未达到目标 Task 所需终态时抛出。
    """
    for dependency_id in task["dependencies"]:
        dependency = tasks_by_id.get(dependency_id)
        if dependency is None:
            raise ValueError(f"Task {task['task_id']} 引用了未知依赖：{dependency_id}")
        if task["task_type"] == "report":
            if dependency["status"] not in {"completed", "failed", "skipped"}:
                raise ValueError(
                    f"Task {task['task_id']} 的依赖尚未进入终态：{dependency_id}"
                )
            continue
        dependency_completed = dependency["status"] == "completed"
        dependency_skipped_normally = dependency["status"] == "skipped" and not dependency.get(
            "error"
        )
        if not dependency_completed and not dependency_skipped_normally:
            raise ValueError(f"Task {task['task_id']} 的依赖尚未就绪：{dependency_id}")
