from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Literal, cast

from app.services.recovery_policy import (
    apply_recovery_policy_to_error,
    copy_recovery_policy_state,
    create_recovery_policy_state,
    recommend_recovery_action,
)
from app.services.task_system import (
    build_task_execution_id,
    build_task_id,
    resolve_error_task,
)
from app.state.models import ErrorContextState, ErrorRecord
from app.utils.runtime import create_error_record, utc_now_iso

"""本模块为业务节点和工具补齐稳定错误归属，并统一应用当前 Recovery Policy。"""


# 错误阶段到六个固定 Task 类型的保守映射，用于调用方未显式提供 Task 时补齐归属。
ERROR_STAGE_TASK_TYPES: dict[str, str] = {
    "inventory": "inventory",
    "content_subagent": "inventory",
    "version_analysis": "version_analysis",
    "version_subagent": "version_analysis",
    "evidence": "evidence",
    "evidence_subagent": "evidence",
    "recommendation": "recommendation",
    "human_review": "human_review",
    "report": "report",
}

# 顶层及团队节点到固定 Task 类型的补充映射，优先级高于宽泛阶段名称。
ERROR_NODE_TASK_TYPES: dict[str, str] = {
    "run_inventory_subgraph": "inventory",
    "sync_inventory_task_status": "inventory",
    "dispatch_content_subagent_task": "inventory",
    "run_version_analysis_subgraph": "version_analysis",
    "sync_version_task_status": "version_analysis",
    "run_evidence_subgraph": "evidence",
    "sync_evidence_task_status": "evidence",
    "dispatch_evidence_subagent_task": "evidence",
    "run_recommendation_subgraph": "recommendation",
    "sync_recommendation_task_status": "recommendation",
    "sync_human_review_task_status": "human_review",
    "generate_governance_report": "report",
    "sync_report_task_status": "report",
}

# Recovery 已经完成处理的错误终态；这些记录只保留审计，不得再次触发顶层恢复。
RESOLVED_ERROR_STATUSES = frozenset({"recovered", "fallback_applied"})


def copy_error_context(context: Mapping[str, Any]) -> ErrorContextState:
    """复制业务子图使用的错误执行上下文。

    Args:
        context: 包含运行、Task、Task 执行和恢复策略的上下文映射。

    Returns:
        与输入解除可变引用关系的完整错误上下文。
    """
    return ErrorContextState(
        run_id=str(context["run_id"]),
        task_id=str(context["task_id"]),
        task_execution_id=str(context["task_execution_id"]),
        policy=copy_recovery_policy_state(context["policy"]),
    )


def create_error_context(
    state: Mapping[str, Any],
    *,
    task_type: str | None = None,
    task_id: str | None = None,
    task_execution_id: str | None = None,
) -> ErrorContextState:
    """从顶层、业务子图或固定 Subagent 状态构造最小错误上下文。

    Args:
        state: 当前节点读取的状态。
        task_type: 可选固定 Task 类型。
        task_id: 可选显式 Task ID。
        task_execution_id: 可选显式 Task 执行 ID。

    Returns:
        具有非空运行、Task、Task 执行 ID 和完整策略的上下文。
    """
    existing = state.get("error_context")
    if isinstance(existing, Mapping):
        copied = copy_error_context(existing)
        if task_id is None and task_execution_id is None:
            return copied
    run_id = str(
        state.get("run", {}).get("run_id")
        or (existing.get("run_id") if isinstance(existing, Mapping) else "")
        or "standalone"
    )
    input_payload = state.get("input")
    input_task_id = (
        str(input_payload.get("task_id"))
        if isinstance(input_payload, Mapping) and input_payload.get("task_id")
        else None
    )
    command = state.get("task_update") or state.get("dispatch_request")
    command_task_id = (
        str(command.get("task_id"))
        if isinstance(command, Mapping) and command.get("task_id")
        else None
    )
    selected_task_id = task_id or input_task_id or command_task_id
    task = resolve_error_task(
        state.get("tasks", []),
        task_type=task_type,
        task_id=selected_task_id,
    )
    if task is not None:
        selected_task_id = str(task["task_id"])
        selected_execution_id = str(task["execution_id"])
    else:
        normalized_task_type = task_type or "lifecycle"
        if normalized_task_type in ERROR_STAGE_TASK_TYPES.values():
            selected_task_id = selected_task_id or build_task_id(
                run_id,
                normalized_task_type,
            )
            selected_execution_id = task_execution_id or build_task_execution_id(
                run_id,
                normalized_task_type,
            )
        else:
            selected_task_id = selected_task_id or f"{run_id}:{normalized_task_type}"
            selected_execution_id = (
                task_execution_id or f"{selected_task_id}:execution"
            )
    if task_execution_id is not None:
        selected_execution_id = task_execution_id
    recovery = state.get("recovery")
    if isinstance(recovery, Mapping) and isinstance(recovery.get("policy"), Mapping):
        policy = copy_recovery_policy_state(recovery["policy"])
    elif isinstance(existing, Mapping) and isinstance(existing.get("policy"), Mapping):
        policy = copy_recovery_policy_state(existing["policy"])
    else:
        policy = create_recovery_policy_state({"enabled": False})
    return ErrorContextState(
        run_id=run_id,
        task_id=str(selected_task_id),
        task_execution_id=str(selected_execution_id),
        policy=policy,
    )


def create_node_execution_id(
    context: Mapping[str, Any],
    node_name: str,
) -> str:
    """为一个捕获错误的内部图节点生成稳定执行标识。

    Args:
        context: 已补齐运行和 Task 执行 ID 的错误上下文。
        node_name: 实际捕获错误的节点函数名。

    Returns:
        可关联 NodeExecutionRecord 的稳定 SHA-256 标识。
    """
    identity = "\x1f".join(
        (
            str(context["run_id"]),
            str(context["task_execution_id"]),
            node_name,
        )
    )
    return "node-error-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def create_node_error(
    state: Mapping[str, Any],
    *,
    stage: str,
    node_name: str,
    category: Literal[
        "filesystem",
        "parse",
        "comparison",
        "evidence",
        "llm",
        "validation",
        "protocol",
        "prompt",
        "hook",
        "memory",
        "skill",
        "context",
        "database",
        "checkpoint",
        "timeout",
        "unknown",
    ],
    message: str,
    related_file_id: str | None = None,
    task_id: str | None = None,
    node_execution_id: str | None = None,
    exception: BaseException | None = None,
    fatal: bool = False,
) -> ErrorRecord:
    """创建只包含脱敏事实、并由 Recovery Policy 决定恢复字段的错误。

    本函数不是 LLM Tool，不执行 I/O、重试或降级。调用节点只提供错误事实；
    函数负责补齐 Task 和节点执行归属，并把当前策略复制到错误记录。启用
    Recovery 时，只要策略仍有动作，错误就保持 pending，等待顶层统一登记。

    Args:
        state: 当前顶层、业务子图或固定 Subagent 状态。
        stage: 错误所属业务阶段。
        node_name: 实际捕获错误的节点函数名。
        category: Recovery Policy 使用的固定错误类别。
        message: 不含正文、凭据或堆栈的简短错误事实。
        related_file_id: 可选关联文件 ID。
        task_id: 可选显式 Task ID；省略时按上下文和固定映射推导。
        node_execution_id: 可选显式节点执行 ID；省略时确定性生成。
        exception: 可选原始异常；只保存异常类型名称，不保存堆栈。
        fatal: Recovery 关闭时沿用的旧版阻断语义。

    Returns:
        具有非空 Task、节点执行、重试、降级和生命周期字段的错误记录。
    """
    task_type = ERROR_NODE_TASK_TYPES.get(
        node_name,
        ERROR_STAGE_TASK_TYPES.get(stage),
    )
    context = create_error_context(
        state,
        task_type=task_type,
        task_id=task_id,
    )
    policy_enabled = bool(context["policy"]["enabled"])
    resolved_node_execution_id = (
        node_execution_id
        or create_node_execution_id(
            context,
            node_name,
        )
    )
    previous_error = next(
        (
            error
            for error in reversed(state.get("errors", []))
            if error.get("node_execution_id") == resolved_node_execution_id
            and error.get("status") not in RESOLVED_ERROR_STATUSES
        ),
        None,
    )
    raw_error = create_error_record(
        stage=stage,
        node_name=node_name,
        category=category,
        message=message,
        related_file_id=related_file_id,
        task_id=context["task_id"],
        node_execution_id=resolved_node_execution_id,
        exception_type=type(exception).__name__ if exception is not None else None,
        status="pending" if policy_enabled else None,
        fatal=fatal,
    )
    enriched = apply_recovery_policy_to_error(raw_error, context["policy"])
    if previous_error is not None:
        enriched = cast(
            ErrorRecord,
            {
                **dict(enriched),
                "retry_count": int(previous_error.get("retry_count", 0)),
                "created_at": str(previous_error.get("created_at") or enriched["created_at"]),
            },
        )
    if not policy_enabled:
        return enriched
    action = recommend_recovery_action(enriched, context["policy"])
    if action == "none":
        return cast(
            ErrorRecord,
            {
                **dict(enriched),
                "status": "recovered",
                "fatal": False,
                "recovered_at": utc_now_iso(),
            },
        )
    return cast(
        ErrorRecord,
        {
            **dict(enriched),
            "status": "pending",
            "fatal": True,
            "recovered_at": None,
        },
    )


def is_error_unresolved(error: Mapping[str, Any]) -> bool:
    """判断错误是否仍应触发顶层 Recovery。

    Args:
        error: 顶层或子图中的结构化错误记录。

    Returns:
        错误为致命且尚未 recovered 或 fallback_applied 时返回 True。
    """
    return bool(
        error.get("fatal") is True
        and error.get("status") not in RESOLVED_ERROR_STATUSES
    )


def has_unresolved_errors(errors: list[ErrorRecord] | tuple[ErrorRecord, ...]) -> bool:
    """判断错误序列中是否存在尚未恢复的阻断错误。

    Args:
        errors: 等待顶层条件路由检查的错误记录序列。

    Returns:
        至少存在一条未解决致命错误时返回 True。
    """
    return any(is_error_unresolved(error) for error in errors)
