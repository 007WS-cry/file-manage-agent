from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

from langgraph.types import interrupt

from app.services.recovery_execution import (
    persist_recovery_error,
    resolve_recovery_targets,
)
from app.services.recovery_policy import (
    apply_recovery_policy_to_error,
    calculate_retry_backoff,
    recommend_recovery_action,
    resolve_category_policy,
)
from app.state.factories import copy_recovery_state
from app.state.models import (
    DegradationRecord,
    ErrorRecord,
    FileGovernanceState,
    RecoveryGraphState,
)
from app.utils.runtime import paths_overlap, utc_now_iso

"""本模块只定义 Error Recovery 子图和顶层恢复续跑选择器明确注册的节点函数。"""


def select_recovery_error(state: RecoveryGraphState) -> dict:
    """选择最新未解决错误并建立固定恢复目标。

    Args:
        state: 包含顶层错误、恢复策略和可选既有队列的恢复子图状态。

    Returns:
        正在恢复的运行状态、策略补齐后的错误和固定跳转目标。

    Raises:
        LookupError: 没有可处理错误或错误无法映射到安全顶层节点时抛出。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    candidate_ids = [
        recovery.get("current_error_id"),
        *reversed(recovery.get("pending_error_ids", [])),
    ]
    error_by_id = {str(error["id"]): error for error in state.get("errors", [])}
    current_error = next(
        (
            error_by_id[error_id]
            for error_id in candidate_ids
            if error_id is not None
            and error_id in error_by_id
            and error_by_id[error_id].get("status") not in {"recovered", "fallback_applied"}
        ),
        None,
    )
    if current_error is None:
        current_error = next(
            (
                error
                for error in reversed(state.get("errors", []))
                if error.get("fatal") is True
                or error.get("status") in {"pending", "retrying", "waiting_human", "failed"}
            ),
            None,
        )
    if current_error is None:
        raise LookupError("Error Recovery 未找到可处理错误")

    normalized_error = apply_recovery_policy_to_error(
        current_error,
        recovery["policy"],
    )
    if normalized_error["status"] == "failed":
        normalized_error["status"] = "pending"
        normalized_error["recovered_at"] = None
    retry_node, resume_after_node = resolve_recovery_targets(
        normalized_error,
        state,
    )
    if retry_node is None or resume_after_node is None:
        normalized_error["status"] = "failed"
        normalized_error["fatal"] = True
        recovery["action"] = "abort"
        recovery["last_policy_reason"] = "错误来源无法映射到安全恢复节点。"
    else:
        recovery["action"] = "none"
        recovery["last_policy_reason"] = "已选择最新未解决错误并加载类别策略。"
    recovery["current_error_id"] = normalized_error["id"]
    recovery["pending_error_ids"] = list(
        dict.fromkeys(
            [
                *recovery["pending_error_ids"],
                normalized_error["id"],
            ]
        )
    )
    recovery["resume_node"] = retry_node
    recovery["resume_after_node"] = resume_after_node

    run = dict(state["run"])
    run.update({"status": "recovering", "current_stage": "error_recovery"})
    working_state = {
        **state,
        "run": run,
        "recovery": recovery,
        "errors": [*state.get("errors", []), normalized_error],
    }
    persist_recovery_error(
        working_state,
        normalized_error,
        action=recovery["action"],
    )
    return {
        "run": run,
        "errors": [normalized_error],
        "recovery": recovery,
    }


def inspect_reusable_execution(state: RecoveryGraphState) -> dict:
    """检查当前错误是否已经具有可复用的成功节点执行。

    本节点只检查状态或短事务预加载得到的执行元数据，不读取结果产物；产物路径
    和摘要会在顶层恢复包装边界再次验证。

    Args:
        state: 已选定当前错误并补充持久化执行记录的恢复状态。

    Returns:
        找到成功记录时标记 ``reuse_result``，否则保持 ``none``。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    error = next(
        (
            item
            for item in state.get("errors", [])
            if item.get("id") == recovery.get("current_error_id")
        ),
        None,
    )
    if error is None:
        raise LookupError("Recovery 当前错误不存在")
    if recovery.get("action") == "abort":
        return {"recovery": recovery}
    execution_id = error.get("node_execution_id")
    execution = next(
        (
            item
            for item in state.get("node_executions", [])
            if item.get("id") == execution_id
            and item.get("status") in {"succeeded", "reused"}
            and item.get("state_update_ref")
            and item.get("result_digest")
        ),
        None,
    )
    if execution is None:
        recovery["action"] = "none"
        recovery["last_policy_reason"] = "未找到可复用的成功节点执行。"
        return {"recovery": recovery}

    recovered_error = cast(
        ErrorRecord,
        {
            **dict(error),
            "status": "recovered",
            "fatal": False,
            "recovered_at": utc_now_iso(),
        },
    )
    recovery["action"] = "reuse_result"
    recovery["last_policy_reason"] = "幂等键和成功执行记录允许复用受控结果。"
    working_state = {
        **state,
        "recovery": recovery,
        "errors": [*state.get("errors", []), recovered_error],
    }
    persist_recovery_error(
        working_state,
        recovered_error,
        action="reuse_result",
    )
    return {
        "errors": [recovered_error],
        "recovery": recovery,
    }


def decide_recovery_action(state: RecoveryGraphState) -> dict:
    """根据策略快照选择重试、降级、人工恢复或终止。

    Args:
        state: 当前错误已完成结果复用检查的恢复状态。

    Returns:
        只包含固定动作和说明的 Recovery 更新。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    error = next(
        (
            item
            for item in state.get("errors", [])
            if item.get("id") == recovery.get("current_error_id")
        ),
        None,
    )
    if error is None:
        raise LookupError("Recovery 当前错误不存在")
    action = recommend_recovery_action(error, recovery["policy"])
    recovery["action"] = action
    recovery["fallback"] = error.get("fallback")
    recovery["last_policy_reason"] = {
        "none": "恢复策略关闭或错误已经解决。",
        "retry": "错误类别允许有限自动重试且仍有剩余次数。",
        "fallback": "自动重试不可用或耗尽，存在固定安全降级。",
        "wait_human": "自动恢复不足，策略允许独立人工恢复。",
        "abort": "没有可用重试、安全降级或人工恢复动作。",
    }[action]
    return {"recovery": recovery}


def schedule_recovery_retry(state: RecoveryGraphState) -> dict:
    """登记一次自动或人工授权的有限重试。

    Args:
        state: 策略或人工选择已经决定重新执行失败节点的恢复状态。

    Returns:
        retrying 错误、重试中的 Task、退避时间和恢复运行状态。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    error = next(
        (
            item
            for item in state.get("errors", [])
            if item.get("id") == recovery.get("current_error_id")
        ),
        None,
    )
    if error is None:
        raise LookupError("Recovery 当前错误不存在")

    retry_count = int(error.get("retry_count", 0)) + 1
    max_retries = int(error.get("max_retries", 0))
    human_retry = recovery["human"].get("selected_action") in {
        "retry",
        "provide_path",
    }
    if retry_count > max_retries:
        if not human_retry:
            raise ValueError("自动重试次数超过策略上限")
        max_retries = retry_count
    category_policy = resolve_category_policy(
        recovery["policy"],
        str(error.get("category", "unknown")),
    )
    retry_delay = (
        0.0
        if human_retry
        else calculate_retry_backoff(
            {**category_policy, "max_retries": max_retries},
            retry_count,
        )
    )
    retrying_error = cast(
        ErrorRecord,
        {
            **dict(error),
            "retryable": True,
            "retry_count": retry_count,
            "max_retries": max_retries,
            "status": "retrying",
            "fatal": False,
            "recovered_at": None,
        },
    )
    tasks = []
    for task in state.get("tasks", []):
        if task.get("task_id") != error.get("task_id"):
            continue
        tasks.append(
            {
                **dict(task),
                "status": "running",
                "attempt_count": int(task.get("attempt_count", 0)) + 1,
                "error": error.get("message"),
                "updated_at": utc_now_iso(),
            }
        )

    recovery["action"] = "retry"
    recovery["retry_delay_seconds"] = retry_delay
    recovery["last_policy_reason"] = (
        "人工已授权重新执行失败节点。"
        if human_retry
        else f"已安排第 {retry_count} 次有限自动重试。"
    )
    recovery["human"] = {
        **recovery["human"],
        "pending_error_id": None,
        "allowed_actions": [],
    }
    run = dict(state["run"])
    run.update({"status": "running", "current_stage": "recovery_retry_scheduled"})
    working_state = {
        **state,
        "run": run,
        "recovery": recovery,
        "errors": [*state.get("errors", []), retrying_error],
    }
    persist_recovery_error(
        working_state,
        retrying_error,
        action="retry",
    )
    return {
        "run": run,
        "tasks": tasks,
        "errors": [retrying_error],
        "recovery": recovery,
    }


def apply_recovery_fallback(state: RecoveryGraphState) -> dict:
    """应用固定安全降级并记录其影响。

    Args:
        state: 策略或人工选择已经确定降级动作的恢复状态。

    Returns:
        非致命错误、部分 Task、降级记录和后继恢复动作。

    Raises:
        ValueError: 当前错误没有白名单内的安全降级时抛出。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    error = next(
        (
            item
            for item in state.get("errors", [])
            if item.get("id") == recovery.get("current_error_id")
        ),
        None,
    )
    if error is None:
        raise LookupError("Recovery 当前错误不存在")
    selected_human_action = recovery["human"].get("selected_action")
    fallback = (
        "skip_file"
        if selected_human_action == "skip_file"
        else recovery.get("fallback") or error.get("fallback")
    )
    if fallback not in {
        "skip_file",
        "coordinator",
        "no_memory",
        "default_skill",
        "keep_context",
        "partial_result",
    }:
        raise ValueError("当前错误没有允许的安全降级")

    created_at = utc_now_iso()
    degradation_id = (
        "degradation-"
        + hashlib.sha256(
            f"{state['run']['run_id']}\x1f{error['id']}\x1f{fallback}".encode()
        ).hexdigest()
    )
    degradation = DegradationRecord(
        id=degradation_id,
        error_id=str(error["id"]),
        stage=str(error["stage"]),
        action=cast(Any, fallback),
        summary=f"恢复流程已应用安全降级：{fallback}。",
        affected_file_ids=(
            [str(error["related_file_id"])] if error.get("related_file_id") is not None else []
        ),
        impact="当前阶段保留可用结果，最终报告将标记为部分完成。",
        created_at=created_at,
    )
    fallback_error = cast(
        ErrorRecord,
        {
            **dict(error),
            "fallback": fallback,
            "status": "fallback_applied",
            "fatal": False,
            "recovered_at": created_at,
        },
    )
    tasks = []
    for task in state.get("tasks", []):
        if task.get("task_id") != error.get("task_id"):
            continue
        tasks.append(
            {
                **dict(task),
                "status": "partial",
                "error": error.get("message"),
                "updated_at": created_at,
            }
        )

    recovery["fallback"] = cast(Any, fallback)
    recovery["action"] = (
        "skip_file"
        if fallback == "skip_file"
        else ("continue_partial" if fallback == "partial_result" else "fallback")
    )
    recovery["degradation_ids"] = list(
        dict.fromkeys([*recovery["degradation_ids"], degradation_id])
    )
    recovery["pending_error_ids"] = [
        error_id for error_id in recovery["pending_error_ids"] if error_id != error["id"]
    ]
    recovery["last_policy_reason"] = f"已应用固定安全降级 {fallback}。"
    run = dict(state["run"])
    run.update({"status": "running", "current_stage": "recovery_fallback_applied"})
    working_state = {
        **state,
        "run": run,
        "recovery": recovery,
        "errors": [*state.get("errors", []), fallback_error],
    }
    persist_recovery_error(
        working_state,
        fallback_error,
        action=recovery["action"],
    )
    return {
        "run": run,
        "tasks": tasks,
        "errors": [fallback_error],
        "degradations": [degradation],
        "recovery": recovery,
    }


def prepare_recovery_human_input(state: RecoveryGraphState) -> dict:
    """在 interrupt 前提交独立的恢复型人工请求状态。

    Args:
        state: 自动恢复动作已经耗尽的恢复状态。

    Returns:
        waiting_human 运行、错误和允许动作。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    error = next(
        (
            item
            for item in state.get("errors", [])
            if item.get("id") == recovery.get("current_error_id")
        ),
        None,
    )
    if error is None:
        raise LookupError("Recovery 当前错误不存在")
    allowed_actions = ["abort"]
    if error.get("related_file_id") is not None:
        allowed_actions.insert(0, "skip_file")
    if error.get("category") in {"filesystem", "validation"}:
        allowed_actions.insert(0, "provide_path")
    if error.get("retryable") is True:
        allowed_actions.insert(0, "retry")

    waiting_error = cast(
        ErrorRecord,
        {
            **dict(error),
            "status": "waiting_human",
            "fatal": False,
        },
    )
    recovery["action"] = "wait_human"
    recovery["human"] = {
        "kind": "error_recovery",
        "pending_error_id": str(error["id"]),
        "allowed_actions": cast(Any, allowed_actions),
        "selected_action": None,
        "replacement_path": None,
        "note": None,
    }
    recovery["last_policy_reason"] = "自动恢复不足，正在等待独立人工恢复输入。"
    run = dict(state["run"])
    run.update({"status": "waiting_human", "current_stage": "error_recovery"})
    working_state = {
        **state,
        "run": run,
        "recovery": recovery,
        "errors": [*state.get("errors", []), waiting_error],
    }
    persist_recovery_error(
        working_state,
        waiting_error,
        action="wait_human",
    )
    return {
        "run": run,
        "errors": [waiting_error],
        "recovery": recovery,
    }


def request_recovery_human_input(state: RecoveryGraphState) -> dict:
    """暂停恢复子图并读取受白名单约束的人工动作。

    Args:
        state: 已提交 waiting_human 状态的恢复子图状态。

    Returns:
        已完成基础类型校验的人工动作、替换路径和简短说明。

    Raises:
        ValueError: 恢复值结构、动作或字段不符合当前请求时抛出。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    human = recovery["human"]
    resume_value: Any = interrupt(
        {
            "kind": "error_recovery",
            "instruction": "请选择允许的错误恢复动作。",
            "error_id": human["pending_error_id"],
            "allowed_actions": list(human["allowed_actions"]),
            "expected_schema": {
                "action": "<allowed_action>",
                "replacement_path": "provide_path 时必填",
                "note": "可选简短说明",
            },
        }
    )
    if not isinstance(resume_value, dict):
        raise ValueError("错误恢复值必须是对象")
    action = resume_value.get("action")
    if action not in human["allowed_actions"]:
        raise ValueError("错误恢复动作不在当前允许列表中")
    replacement_path = resume_value.get("replacement_path")
    if action == "provide_path":
        if not isinstance(replacement_path, str) or not replacement_path.strip():
            raise ValueError("provide_path 必须提供非空 replacement_path")
    elif replacement_path is not None:
        raise ValueError("非 provide_path 动作不得携带 replacement_path")
    note = resume_value.get("note")
    if note is not None and (not isinstance(note, str) or len(note.strip()) > 500):
        raise ValueError("note 必须是最多 500 字符的字符串或 null")
    human["selected_action"] = cast(Any, action)
    human["replacement_path"] = (
        replacement_path.strip() if isinstance(replacement_path, str) else None
    )
    human["note"] = note.strip() if isinstance(note, str) else None
    recovery["human"] = human
    return {"recovery": recovery}


def apply_recovery_human_input(state: RecoveryGraphState) -> dict:
    """应用已经校验的人工恢复输入，但不直接决定业务降级。

    Args:
        state: 已从恢复型 interrupt 返回的恢复状态。

    Returns:
        可选路径修正、恢复动作提示和重新运行状态。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    action = recovery["human"].get("selected_action")
    if action is None:
        raise ValueError("人工恢复尚未选择动作")
    request = dict(state["request"])
    workspace = dict(state["workspace"])
    if action == "provide_path":
        replacement_path = recovery["human"].get("replacement_path")
        if replacement_path is None:
            raise ValueError("provide_path 缺少 replacement_path")
        original_path = Path(replacement_path).expanduser()
        if original_path.is_symlink():
            raise ValueError("人工替换路径不得是符号链接")
        resolved_path = original_path.resolve(strict=True)
        if not resolved_path.is_dir():
            raise ValueError("人工替换路径必须是可读取目录")
        for output_name in ("artifact_root", "report_root"):
            output_path = workspace.get(output_name)
            if output_path and paths_overlap(resolved_path, output_path):
                raise ValueError(f"人工替换路径不得与 {output_name} 相同或互为上下级目录")
        database = state.get("application_database", {})
        for database_name in ("database_path", "checkpoint_path"):
            database_path = database.get(database_name)
            if database_path and paths_overlap(resolved_path, database_path):
                raise ValueError(f"人工替换路径不得包含 {database_name}")
        request["root_directory"] = str(resolved_path)
        workspace["input_root"] = str(resolved_path)
        workspace["input_readonly"] = True
    if action == "skip_file":
        recovery["fallback"] = "skip_file"
    recovery["last_policy_reason"] = f"人工已选择恢复动作 {action}。"
    run = dict(state["run"])
    run.update({"status": "recovering", "current_stage": "error_recovery"})
    return {
        "run": run,
        "request": request,
        "workspace": workspace,
        "recovery": recovery,
    }


def mark_recovery_aborted(state: RecoveryGraphState) -> dict:
    """把当前错误标记为最终失败并结束自动恢复。

    Args:
        state: 策略或人工动作已经选择终止的恢复状态。

    Returns:
        failed 错误、失败 Task、abort 动作和恢复运行状态。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    error = next(
        (
            item
            for item in state.get("errors", [])
            if item.get("id") == recovery.get("current_error_id")
        ),
        None,
    )
    if error is None:
        raise LookupError("Recovery 当前错误不存在")
    failed_error = cast(
        ErrorRecord,
        {
            **dict(error),
            "status": "failed",
            "fatal": True,
            "recovered_at": None,
        },
    )
    tasks = []
    for task in state.get("tasks", []):
        if task.get("task_id") != error.get("task_id"):
            continue
        tasks.append(
            {
                **dict(task),
                "status": "failed",
                "error": error.get("message"),
                "updated_at": utc_now_iso(),
            }
        )
    recovery["action"] = "abort"
    recovery["pending_error_ids"] = [
        error_id for error_id in recovery["pending_error_ids"] if error_id != error["id"]
    ]
    recovery["last_policy_reason"] = "恢复动作已终止，错误保持致命状态。"
    run = dict(state["run"])
    run.update({"status": "recovering", "current_stage": "recovery_aborted"})
    working_state = {
        **state,
        "run": run,
        "recovery": recovery,
        "errors": [*state.get("errors", []), failed_error],
    }
    persist_recovery_error(
        working_state,
        failed_error,
        action="abort",
    )
    return {
        "run": run,
        "tasks": tasks,
        "errors": [failed_error],
        "recovery": recovery,
    }


def finalize_recovery_outcome(state: RecoveryGraphState) -> dict:
    """规范化恢复子图终点，保留顶层条件路由所需动作。

    Args:
        state: 已完成重试安排、结果复用、安全降级或终止的恢复状态。

    Returns:
        清理人工等待标记后的运行和 Recovery 状态。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    if recovery["action"] == "wait_human":
        raise ValueError("人工恢复尚未完成，不能结束恢复子图")
    human = dict(recovery["human"])
    human["pending_error_id"] = None
    human["allowed_actions"] = []
    recovery["human"] = human
    run = dict(state["run"])
    if recovery["action"] != "abort":
        run.update({"status": "running", "current_stage": "recovery_complete"})
    return {"run": run, "recovery": recovery}


def select_resume_after_failed_stage(state: FileGovernanceState) -> dict:
    """为顶层第二段条件路由提供无副作用的稳定检查点。

    Args:
        state: 已从 Error Recovery 子图返回的顶层兼容状态。

    Returns:
        空更新；后继节点完全由 ``resume_after_failed_stage`` 条件路由选择。
    """
    del state
    return {}
