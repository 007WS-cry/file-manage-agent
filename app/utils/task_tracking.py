from __future__ import annotations

from typing import Literal, cast

from app.graphs.team_orchestration import team_orchestration_graph
from app.services.task_system import build_task_id
from app.state.converters import (
    file_governance_to_team_orchestration_state,
    team_orchestration_state_to_file_governance_update,
)
from app.state.models import (
    ContentSubagentInput,
    ErrorRecord,
    EvidenceSubagentInput,
    FileGovernanceState,
    TaskItem,
    TaskStatusUpdate,
    VersionSubagentInput,
)
from app.utils.runtime import utc_now_iso

"""本模块提供顶层 Task 跟踪节点使用的编排调用、状态转换和结果收敛辅助能力。"""

# 六个固定 Task 的执行顺序；报告阶段负责统一收口成功、无数据和失败路径。
TASK_EXECUTION_ORDER: tuple[str, ...] = (
    "inventory",
    "version_analysis",
    "evidence",
    "recommendation",
    "human_review",
    "report",
)

# 四个业务 Task 完成后登记的顶层状态产物引用。
BUSINESS_OUTPUT_REFS: dict[str, tuple[str, ...]] = {
    "inventory": ("files", "documents"),
    "version_analysis": (
        "version_groups",
        "diffs",
        "version_edges",
        "branches",
        "version_chains",
    ),
    "evidence": ("pdf_exports", "deliveries"),
    "recommendation": ("decisions", "human_review"),
}


def run_team_orchestration_subgraph(
    state: FileGovernanceState,
    *,
    task_update: TaskStatusUpdate | None = None,
    dispatch_request: (
        ContentSubagentInput
        | VersionSubagentInput
        | EvidenceSubagentInput
        | None
    ) = None,
) -> dict:
    """显式转换状态并执行一次 Task 同步或固定 Subagent 分派。

    Args:
        state: 顶层文件治理状态。
        task_update: 本次调用需要消费的可选 Task 状态更新命令。
        dispatch_request: 本次调用需要消费的可选固定 Subagent 最小输入。

    Returns:
        不包含状态命令和分派私有字段的顶层白名单更新。
    """
    subgraph_input = file_governance_to_team_orchestration_state(
        state,
        task_update=task_update,
        dispatch_request=dispatch_request,
    )
    subgraph_result = team_orchestration_graph.invoke(subgraph_input)
    return team_orchestration_state_to_file_governance_update(subgraph_result)


def _task_by_type(
    state: FileGovernanceState,
    task_type: str,
) -> TaskItem | None:
    """从顶层状态中查找指定类型的 Task。

    Args:
        state: 当前顶层文件治理状态。
        task_type: 等待查找的固定 Task 类型。

    Returns:
        找到时返回 Task；DAG 尚未创建或类型不存在时返回 None。
    """
    return next(
        (task for task in state.get("tasks", []) if task.get("task_type") == task_type),
        None,
    )


def _fatal_errors_for_stage(
    state: FileGovernanceState,
    stage: str,
) -> list[ErrorRecord]:
    """提取某一业务阶段产生的致命错误。

    Args:
        state: 已合并业务子图结果的顶层治理状态。
        stage: 需要检查的业务阶段名称。

    Returns:
        stage 完全匹配且 fatal 为真的错误列表。
    """
    return [
        error
        for error in state.get("errors", [])
        if error.get("stage") == stage and error.get("fatal") is True
    ]


def _format_failure_message(stage: str, errors: list[ErrorRecord]) -> str:
    """为失败 Task 合并稳定且可读的错误摘要。

    Args:
        stage: 失败的 Task 类型或业务阶段。
        errors: 当前阶段的一个或多个致命错误。

    Returns:
        用中文分号连接的错误消息；缺少消息时返回阶段级兜底说明。
    """
    messages = [
        str(error.get("message", "")).strip()
        for error in errors
        if str(error.get("message", "")).strip()
    ]
    return "；".join(messages) if messages else f"{stage} 阶段执行失败"


def has_orchestration_failure(errors: list[ErrorRecord]) -> bool:
    """判断一次 Task 编排调用是否产生致命错误。

    Args:
        errors: 本轮 Team Orchestration 子图新返回的错误。

    Returns:
        存在致命编排错误时返回 True，否则返回 False。
    """
    return any(
        error.get("stage") == "team_orchestration" and error.get("fatal") is True
        for error in errors
    )


def apply_task_status(
    state: FileGovernanceState,
    task_type: str,
    status: Literal["running", "completed", "failed", "skipped"],
    *,
    output_refs: tuple[str, ...] = (),
    error: str | None = None,
) -> tuple[FileGovernanceState, list[ErrorRecord]]:
    """通过独立 Team Orchestration 子图幂等更新一个 Task。

    completed 更新遇到 pending Task 时会先补一次 running，使所有状态转换仍遵循
    Task System 协议。已经处于任一终态的 Task 不会被重新打开或改写时间。

    Args:
        state: 当前顶层治理状态。
        task_type: 等待更新的固定 Task 类型。
        status: 目标 Task 状态。
        output_refs: 本次完成产生的顶层状态字段引用。
        error: failed 或阻断性 skipped 状态使用的错误说明。

    Returns:
        合并最新 Task、Todo 的工作状态，以及本次新产生的编排错误。
    """
    current = _task_by_type(state, task_type)
    if current is not None:
        current_status = current["status"]
        if current_status == status:
            return state, []
        if current_status in {"completed", "failed", "skipped"}:
            return state, []
        if status == "completed" and current_status == "pending":
            running_state, running_errors = apply_task_status(
                state,
                task_type,
                "running",
            )
            if has_orchestration_failure(running_errors):
                return running_state, running_errors
            completed_state, completed_errors = apply_task_status(
                running_state,
                task_type,
                "completed",
                output_refs=output_refs,
            )
            return completed_state, [*running_errors, *completed_errors]

    update = TaskStatusUpdate(
        task_id=build_task_id(state["run"]["run_id"], task_type),
        status=status,
        output_refs=list(output_refs),
        error=error,
        updated_at=utc_now_iso(),
    )
    public_update = run_team_orchestration_subgraph(state, task_update=update)
    working_state = cast(
        FileGovernanceState,
        {
            **state,
            "tasks": public_update["tasks"],
            "todos": public_update["todos"],
        },
    )
    return working_state, list(public_update.get("errors", []))


def public_task_update(
    state: FileGovernanceState,
    errors: list[ErrorRecord],
) -> dict:
    """把内部工作状态收敛为顶层节点允许写回的公开字段。

    Args:
        state: 已应用全部 Task 转换的内部工作状态。
        errors: 本节点调用编排子图时新产生的错误。

    Returns:
        完整 Task、由 Task 推导的 Todo 和新增错误。
    """
    return {
        "tasks": list(state.get("tasks", [])),
        "todos": list(state.get("todos", [])),
        "errors": errors,
    }


def _has_analyzable_documents(state: FileGovernanceState) -> bool:
    """判断 Inventory 是否产生至少一个可进入版本分析的文档。

    Args:
        state: 已合并 Inventory 子图结果的顶层治理状态。

    Returns:
        存在解析成功且具有标准化文档记录的文件时返回 True。
    """
    parsed_file_ids = {
        file_record["id"]
        for file_record in state.get("files", [])
        if file_record.get("parse_status") == "parsed"
    }
    return any(
        document.get("file_id") in parsed_file_ids for document in state.get("documents", [])
    )


def _needs_human_review(state: FileGovernanceState) -> bool:
    """判断推荐结果中是否存在需要人工选择的版本组。

    Args:
        state: 已合并 Recommendation 子图结果的顶层治理状态。

    Returns:
        任一推荐标记 needs_human_review 时返回 True。
    """
    return any(
        decision.get("needs_human_review") is True for decision in state.get("decisions", [])
    )


def _block_downstream_tasks(
    state: FileGovernanceState,
    failed_task_type: str,
    failure_message: str,
) -> tuple[FileGovernanceState, list[ErrorRecord]]:
    """把失败业务 Task 之后、报告之前的 Task 标记为阻断跳过。

    Args:
        state: 已把当前业务 Task 标记为 failed 的工作状态。
        failed_task_type: 实际执行失败的 Task 类型。
        failure_message: 写入失败 Task 的错误摘要。

    Returns:
        下游 Task 已跳过的工作状态和新增编排错误。
    """
    working_state = state
    new_errors: list[ErrorRecord] = []
    failed_index = TASK_EXECUTION_ORDER.index(failed_task_type)
    blocking_reason = f"被上游 {failed_task_type} 失败阻断：{failure_message}"
    for task_type in TASK_EXECUTION_ORDER[failed_index + 1 : -1]:
        working_state, update_errors = apply_task_status(
            working_state,
            task_type,
            "skipped",
            error=blocking_reason,
        )
        new_errors.extend(update_errors)
        if has_orchestration_failure(update_errors):
            break
    return working_state, new_errors


def sync_business_task_status(
    state: FileGovernanceState,
    task_type: str,
    *,
    next_task_type: str | None,
) -> dict:
    """同步一个业务子图结果并在成功时启动确定的下一阶段。

    Args:
        state: 已合并当前业务子图结果的顶层治理状态。
        task_type: 当前业务 Task 类型，同时也是错误 stage。
        next_task_type: 成功后等待启动的下一 Task；无后继时为 None。

    Returns:
        当前和后续 Task 状态、Todo 纯投影以及可选编排错误。
    """
    working_state = state
    new_errors: list[ErrorRecord] = []
    fatal_errors = _fatal_errors_for_stage(state, task_type)
    if fatal_errors:
        failure_message = _format_failure_message(task_type, fatal_errors)
        working_state, update_errors = apply_task_status(
            working_state,
            task_type,
            "failed",
            error=failure_message,
        )
        new_errors.extend(update_errors)
        if not has_orchestration_failure(update_errors):
            working_state, blocked_errors = _block_downstream_tasks(
                working_state,
                task_type,
                failure_message,
            )
            new_errors.extend(blocked_errors)
        return public_task_update(working_state, new_errors)

    working_state, update_errors = apply_task_status(
        working_state,
        task_type,
        "completed",
        output_refs=BUSINESS_OUTPUT_REFS[task_type],
    )
    new_errors.extend(update_errors)
    if has_orchestration_failure(update_errors):
        return public_task_update(working_state, new_errors)

    if task_type == "inventory" and not _has_analyzable_documents(state):
        return public_task_update(working_state, new_errors)

    if task_type == "recommendation":
        human_status: Literal["running", "skipped"] = (
            "running" if _needs_human_review(state) else "skipped"
        )
        working_state, human_errors = apply_task_status(
            working_state,
            "human_review",
            human_status,
        )
        new_errors.extend(human_errors)
    elif next_task_type is not None:
        working_state, next_errors = apply_task_status(
            working_state,
            next_task_type,
            "running",
        )
        new_errors.extend(next_errors)
    return public_task_update(working_state, new_errors)


def _find_upstream_blocker(state: FileGovernanceState) -> TaskItem | None:
    """查找报告之前首个失败或被失败依赖阻断的 Task。

    Args:
        state: 已生成成功、无数据或失败报告的顶层治理状态。

    Returns:
        首个阻断 Task；不存在阻断状态时返回 None。
    """
    for task_type in TASK_EXECUTION_ORDER[:-1]:
        task = _task_by_type(state, task_type)
        if task is not None and (
            task["status"] == "failed" or (task["status"] == "skipped" and bool(task.get("error")))
        ):
            return task
    return None


def settle_unfinished_tasks_before_report(
    state: FileGovernanceState,
) -> tuple[FileGovernanceState, list[ErrorRecord]]:
    """在报告 Task 启动前确定性跳过所有未执行的上游 Task。

    无数据路径使用无错误 skipped，使 Todo 正常完成；失败路径使用带错误 skipped，
    让下游 Todo 显示 blocked，同时不会把被阻断 Task 误报为 failed。

    Args:
        state: 已生成报告且具有合法固定 Task DAG 的顶层状态。

    Returns:
        所有报告前 Task 均进入终态的工作状态和新增编排错误。
    """
    working_state = state
    new_errors: list[ErrorRecord] = []
    blocker = _find_upstream_blocker(state)
    blocking_reason = None
    if blocker is not None:
        blocking_reason = (
            f"被上游 {blocker['task_type']} 失败阻断：{blocker.get('error') or '上游任务失败'}"
        )
    for task_type in TASK_EXECUTION_ORDER[:-1]:
        task = _task_by_type(working_state, task_type)
        if task is None or task["status"] not in {"pending", "running"}:
            continue
        working_state, update_errors = apply_task_status(
            working_state,
            task_type,
            "skipped",
            error=blocking_reason,
        )
        new_errors.extend(update_errors)
        if has_orchestration_failure(update_errors):
            break
    return working_state, new_errors
