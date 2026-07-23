from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import cast

from app.agents.protocol import (
    TeamProtocolError,
    create_result_message,
    validate_content_subagent_input,
    validate_evidence_subagent_input,
    validate_version_subagent_input,
)
from app.agents.protocol import (
    create_assignment_message as create_protocol_assignment_message,
)
from app.agents.protocol import (
    validate_team_message as validate_protocol_message,
)
from app.agents.registry import (
    resolve_fixed_subagent_for_task,
)
from app.graphs.content_subagent import content_subagent_graph
from app.graphs.evidence_subagent import evidence_subagent_graph
from app.graphs.version_subagent import version_subagent_graph
from app.services.task_system import (
    assign_tasks_to_roles as assign_roles,
)
from app.services.task_system import (
    create_task_dag as create_fixed_task_dag,
)
from app.services.task_system import resolve_subagent_task
from app.services.task_system import (
    update_todos_from_tasks as project_todos_from_tasks,
)
from app.services.task_system import (
    validate_task_dag as validate_fixed_task_dag,
)
from app.state.models import (
    ContentSubagentGraphState,
    EvidenceSubagentGraphState,
    LLMCallRecord,
    TaskItem,
    TeamOrchestrationGraphState,
    VersionSubagentGraphState,
)
from app.utils.runtime import utc_now_iso
from app.utils.task_orchestration import (
    ALLOWED_TASK_TRANSITIONS,
    create_dispatch_error,
    create_orchestration_error,
    ensure_task_dependencies_ready,
    find_latest_subagent_message,
    merge_task_output_refs,
    normalize_fixed_team,
    resolve_task_creation_time,
    update_team_dispatch_status,
)

"""本模块只定义 Team Orchestration 图中实际注册的 Task 同步和 Subagent 分派节点。"""

# 三个编排 assignment 使用与角色子图一致的稳定摘要，确保消息 ID 可幂等合并。
ASSIGNMENT_SUMMARY_BY_ROLE: dict[str, str] = {
    "content": "分配内容摘要与关键字段解释任务，输入仅包含短预览和受控引用。",
    "version": "分配版本差异解释任务，输入仅包含比较结果、信号和受控引用。",
    "evidence": "分配外部证据解释任务，输入仅包含 PDF、发送摘要和受控引用。",
}


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
    """为合法 Task DAG 写入可用于实际分派的固定角色。

    Args:
        state: 已通过 DAG 校验的团队编排状态。

    Returns:
        角色已校正的 Task 列表；无法分配时返回结构化致命错误。
    """
    try:
        return {"tasks": assign_roles(state.get("tasks", []))}
    except (KeyError, TypeError, ValueError) as error:
        return {"errors": [create_orchestration_error("assign_tasks_to_roles", error)]}


def initialize_fixed_agent_team(state: TeamOrchestrationGraphState) -> dict:
    """初始化或校验协调者与三个固定 Subagent 的团队状态。

    Args:
        state: 已通过 Task DAG 校验且可选携带已有 TeamState 的编排状态。

    Returns:
        固定成员和协议字段的独立 TeamState；非法团队返回结构化致命错误。
    """
    try:
        return {"team": normalize_fixed_team(state.get("team"))}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "errors": [
                create_orchestration_error("initialize_fixed_agent_team", error)
            ]
        }


def validate_orchestration_action(state: TeamOrchestrationGraphState) -> dict:
    """确认一次编排调用只执行状态同步或单个 Subagent 分派。

    Args:
        state: 已初始化固定团队并完成 Task 角色分配的编排状态。

    Returns:
        状态同步时清除上次私有分派结果；命令冲突时返回结构化致命错误。
    """
    if state.get("task_update") is not None and state.get("dispatch_request") is not None:
        return {
            "errors": [
                create_orchestration_error(
                    "validate_orchestration_action",
                    ValueError("单次 Team Orchestration 调用不能同时同步状态和分派 Subagent"),
                )
            ]
        }
    if state.get("dispatch_request") is None:
        return {"dispatch_result": None}
    return {}


def validate_subagent_payload(state: TeamOrchestrationGraphState) -> dict:
    """校验分派请求、真实 Task、固定角色和最小 Subagent 输入协议。

    Args:
        state: 包含完整 Task DAG 和单个 dispatch_request 的编排状态。

    Returns:
        规范化后的角色专属输入；非法载荷返回可由协调者处理的非致命错误。
    """
    try:
        request = state.get("dispatch_request")
        if not isinstance(request, Mapping):
            raise TeamProtocolError("dispatch_request 必须是 Subagent 最小输入对象")
        task_id = request.get("task_id")
        if not isinstance(task_id, str):
            raise TeamProtocolError("dispatch_request.task_id 必须是字符串")
        task = resolve_subagent_task(state.get("tasks", []), task_id)
        definition = resolve_fixed_subagent_for_task(task["task_type"])
        if task["assigned_role"] != definition.role:
            raise TeamProtocolError("Task assigned_role 与固定 Subagent 不一致")

        if definition.role == "content":
            normalized = validate_content_subagent_input(request)
        elif definition.role == "version":
            normalized = validate_version_subagent_input(request)
        else:
            normalized = validate_evidence_subagent_input(request)
        return {"dispatch_request": normalized, "dispatch_result": None}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "dispatch_result": None,
            "errors": [create_dispatch_error("validate_subagent_payload", error)],
        }


def create_assignment_message(state: TeamOrchestrationGraphState) -> dict:
    """创建 coordinator 发给目标固定 Subagent 的 assignment 消息。

    Args:
        state: 已通过最小输入、Task 和角色校验的编排状态。

    Returns:
        已验证 assignment 消息及 waiting/working 团队状态；失败时返回分派错误。
    """
    try:
        request = state["dispatch_request"]
        if request is None:
            raise TeamProtocolError("创建 assignment 前缺少 dispatch_request")
        task = resolve_subagent_task(state.get("tasks", []), request["task_id"])
        definition = resolve_fixed_subagent_for_task(task["task_type"])
        message = create_protocol_assignment_message(
            team=state["team"],
            task_id=task["task_id"],
            receiver=definition.agent_id,
            summary=ASSIGNMENT_SUMMARY_BY_ROLE[definition.role],
            artifact_refs=request["artifact_refs"],
        )
        team = update_team_dispatch_status(
            state["team"],
            agent_id=definition.agent_id,
            task_id=task["task_id"],
            active=True,
        )
        return {"team": team, "team_messages": [message]}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "errors": [create_dispatch_error("create_assignment_message", error)]
        }


def invoke_content_subagent_graph(state: TeamOrchestrationGraphState) -> dict:
    """把已验证 Content 分派转换为独立子图状态并同步调用该子图。

    Args:
        state: 已创建 assignment 且角色确定为 content 的编排状态。

    Returns:
        Content 结构化结果、Team Message、LLM 审计和可选非致命错误。
    """
    try:
        request = state["dispatch_request"]
        if request is None or "document_id" not in request:
            raise TeamProtocolError("Content 分派请求类型不匹配")
        assignments = [
            dict(message)
            for message in state.get("team_messages", [])
            if message.get("task_id") == request["task_id"]
            and message.get("message_type") == "assignment"
        ]
        subgraph_state = ContentSubagentGraphState(
            input=cast(dict, request),
            team=normalize_fixed_team(state["team"]),
            llm=dict(state["llm"]),
            skill_context=[
                dict(instruction)
                for instruction in state.get("skill_context", [])
            ],
            selected_model_profile_id="",
            system_prompt="",
            user_prompt="",
            output=None,
            fallback_used=False,
            team_messages=assignments,
            llm_calls=[],
            errors=[],
        )
        result = content_subagent_graph.invoke(subgraph_state)
        return {
            "dispatch_result": result.get("output"),
            "team_messages": list(result.get("team_messages", [])),
            "llm_calls": list(result.get("llm_calls", [])),
            "errors": list(result.get("errors", [])),
        }
    except Exception as error:
        return {
            "dispatch_result": None,
            "errors": [
                create_dispatch_error(
                    "invoke_content_subagent_graph",
                    RuntimeError(f"{type(error).__name__}: Content Subagent 子图调用失败"),
                )
            ],
        }


def invoke_version_subagent_graph(state: TeamOrchestrationGraphState) -> dict:
    """把已验证 Version 分派转换为独立子图状态并同步调用该子图。

    Args:
        state: 已创建 assignment 且角色确定为 version 的编排状态。

    Returns:
        Version 结构化结果、Team Message、LLM 审计和可选非致命错误。
    """
    try:
        request = state["dispatch_request"]
        if request is None or "comparison_id" not in request:
            raise TeamProtocolError("Version 分派请求类型不匹配")
        assignments = [
            dict(message)
            for message in state.get("team_messages", [])
            if message.get("task_id") == request["task_id"]
            and message.get("message_type") == "assignment"
        ]
        subgraph_state = VersionSubagentGraphState(
            input=cast(dict, request),
            team=normalize_fixed_team(state["team"]),
            llm=dict(state["llm"]),
            skill_context=[
                dict(instruction)
                for instruction in state.get("skill_context", [])
            ],
            selected_model_profile_id="",
            system_prompt="",
            user_prompt="",
            output=None,
            fallback_used=False,
            team_messages=assignments,
            llm_calls=[],
            errors=[],
        )
        result = version_subagent_graph.invoke(subgraph_state)
        return {
            "dispatch_result": result.get("output"),
            "team_messages": list(result.get("team_messages", [])),
            "llm_calls": list(result.get("llm_calls", [])),
            "errors": list(result.get("errors", [])),
        }
    except Exception as error:
        return {
            "dispatch_result": None,
            "errors": [
                create_dispatch_error(
                    "invoke_version_subagent_graph",
                    RuntimeError(f"{type(error).__name__}: Version Subagent 子图调用失败"),
                )
            ],
        }


def invoke_evidence_subagent_graph(state: TeamOrchestrationGraphState) -> dict:
    """把已验证 Evidence 分派转换为独立子图状态并同步调用该子图。

    Args:
        state: 已创建 assignment 且角色确定为 evidence 的编排状态。

    Returns:
        Evidence 结构化结果、Team Message、LLM 审计和可选非致命错误。
    """
    try:
        request = state["dispatch_request"]
        if request is None or "group_id" not in request:
            raise TeamProtocolError("Evidence 分派请求类型不匹配")
        assignments = [
            dict(message)
            for message in state.get("team_messages", [])
            if message.get("task_id") == request["task_id"]
            and message.get("message_type") == "assignment"
        ]
        subgraph_state = EvidenceSubagentGraphState(
            input=cast(dict, request),
            team=normalize_fixed_team(state["team"]),
            llm=dict(state["llm"]),
            skill_context=[
                dict(instruction)
                for instruction in state.get("skill_context", [])
            ],
            selected_model_profile_id="",
            system_prompt="",
            user_prompt="",
            output=None,
            fallback_used=False,
            team_messages=assignments,
            llm_calls=[],
            errors=[],
        )
        result = evidence_subagent_graph.invoke(subgraph_state)
        return {
            "dispatch_result": result.get("output"),
            "team_messages": list(result.get("team_messages", [])),
            "llm_calls": list(result.get("llm_calls", [])),
            "errors": list(result.get("errors", [])),
        }
    except Exception as error:
        return {
            "dispatch_result": None,
            "errors": [
                create_dispatch_error(
                    "invoke_evidence_subagent_graph",
                    RuntimeError(f"{type(error).__name__}: Evidence Subagent 子图调用失败"),
                )
            ],
        }


def validate_team_message(state: TeamOrchestrationGraphState) -> dict:
    """校验 Subagent 最新响应的成员、类型、结果内容和引用白名单。

    Args:
        state: 已完成一个固定 Subagent 子图调用的编排状态。

    Returns:
        合法 result 消息和结构化输出；error 或非法消息会清空结果并记录错误。
    """
    try:
        request = state["dispatch_request"]
        if request is None:
            raise TeamProtocolError("校验 Subagent 消息前缺少 dispatch_request")
        task = resolve_subagent_task(state.get("tasks", []), request["task_id"])
        definition = resolve_fixed_subagent_for_task(task["task_type"])
        response = find_latest_subagent_message(
            state.get("team_messages", []),
            task_id=task["task_id"],
            agent_id=definition.agent_id,
        )
        if response is None:
            raise TeamProtocolError("Subagent 未返回 result 或 error Team Message")
        validated = validate_protocol_message(
            response,
            team=state["team"],
            allowed_artifact_refs=request["artifact_refs"],
        )
        if validated["message_type"] == "error":
            raise TeamProtocolError(validated["error"] or "Subagent 返回 error 消息")

        output = state.get("dispatch_result")
        if output is None or not isinstance(output, definition.output_model):
            raise TeamProtocolError("Subagent Team Message 缺少匹配的结构化结果")
        if validated["summary"] != output.summary:
            raise TeamProtocolError("Team Message.summary 与结构化结果不一致")
        if validated["artifact_refs"] != output.artifact_refs:
            raise TeamProtocolError("Team Message.artifact_refs 与结构化结果不一致")
        return {"dispatch_result": output.model_copy(deep=True), "team_messages": [validated]}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "dispatch_result": None,
            "errors": [create_dispatch_error("validate_team_message", error)],
        }


def fallback_to_coordinator(state: TeamOrchestrationGraphState) -> dict:
    """在分派或返回协议失败时使用固定角色的确定性逻辑生成结果。

    Args:
        state: Subagent 调用失败、返回 error 或消息校验失败的编排状态。

    Returns:
        不读取正文的确定性 Pydantic 输出和状态为 fallback 的最小审计记录。
    """
    try:
        request = state["dispatch_request"]
        if request is None:
            raise TeamProtocolError("协调者回退前缺少 dispatch_request")
        task = resolve_subagent_task(state.get("tasks", []), request["task_id"])
        definition = resolve_fixed_subagent_for_task(task["task_type"])
        try:
            if definition.role == "content":
                normalized = validate_content_subagent_input(request)
            elif definition.role == "version":
                normalized = validate_version_subagent_input(request)
            else:
                normalized = validate_evidence_subagent_input(request)
            output = definition.fallback_builder(normalized)
        except (KeyError, TypeError, ValueError):
            output = definition.output_model(
                summary=(
                    f"{task['task_type']} Subagent 分派载荷未通过协议校验；"
                    "协调者保留原有确定性治理结果，未读取请求中的未知内容。"
                ),
                artifact_refs=[],
            )

        matching_calls = [
            call
            for call in state.get("llm_calls", [])
            if call.get("task_id") == task["task_id"]
            and call.get("agent_id") == definition.agent_id
        ]
        if matching_calls:
            call_record = dict(matching_calls[-1])
            call_record["status"] = "fallback"
            call_record["fallback_used"] = True
        else:
            timestamp = utc_now_iso()
            assignment = next(
                (
                    message
                    for message in reversed(state.get("team_messages", []))
                    if message.get("task_id") == task["task_id"]
                    and message.get("message_type") == "assignment"
                ),
                None,
            )
            identity = hashlib.sha256(
                f"{task['task_id']}:{definition.agent_id}:coordinator".encode()
            ).hexdigest()
            call_record = LLMCallRecord(
                id=f"llm-coordinator-fallback-{identity}",
                task_id=task["task_id"],
                agent_id=definition.agent_id,
                message_id=(
                    assignment["message_id"]
                    if assignment is not None
                    else "coordinator-fallback"
                ),
                model_profile_id="coordinator-deterministic-fallback",
                provider="deterministic",
                model=f"coordinator-{definition.role}-fallback",
                status="fallback",
                started_at=timestamp,
                finished_at=timestamp,
                duration_ms=0,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                error_type=None,
                error_message=None,
                fallback_used=True,
            )
        return {
            "dispatch_result": output,
            "llm_calls": [cast(LLMCallRecord, call_record)],
        }
    except (KeyError, TypeError, ValueError) as error:
        return {
            "dispatch_result": None,
            "errors": [create_dispatch_error("fallback_to_coordinator", error, fatal=True)],
        }


def build_fallback_result_message(state: TeamOrchestrationGraphState) -> dict:
    """把协调者确定性回退结果包装为固定分派通道的合法 result 消息。

    Args:
        state: 已生成确定性 dispatch_result 的编排状态。

    Returns:
        通过 Team Protocol 校验的 result 消息和恢复为空闲的团队状态。
    """
    try:
        request = state["dispatch_request"]
        output = state.get("dispatch_result")
        if request is None or output is None:
            raise TeamProtocolError("构造回退 result 消息前缺少请求或结果")
        task = resolve_subagent_task(state.get("tasks", []), request["task_id"])
        definition = resolve_fixed_subagent_for_task(task["task_type"])
        message = create_result_message(
            team=state["team"],
            task_id=task["task_id"],
            sender=definition.agent_id,
            summary=output.summary,
            artifact_refs=output.artifact_refs,
        )
        team = update_team_dispatch_status(
            state["team"],
            agent_id=definition.agent_id,
            task_id=task["task_id"],
            active=False,
        )
        return {"team": team, "team_messages": [message]}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "errors": [
                create_dispatch_error("build_fallback_result_message", error, fatal=True)
            ]
        }


def merge_subagent_artifacts(state: TeamOrchestrationGraphState) -> dict:
    """收敛已验证的摘要、受控引用和固定团队运行状态。

    Args:
        state: 已通过返回协议校验或已生成回退 result 消息的编排状态。

    Returns:
        深复制的结构化结果、规范消息以及恢复为空闲的 TeamState。
    """
    try:
        request = state["dispatch_request"]
        output = state.get("dispatch_result")
        if request is None or output is None:
            raise TeamProtocolError("合并 Subagent 产物前缺少请求或结构化结果")
        task = resolve_subagent_task(state.get("tasks", []), request["task_id"])
        definition = resolve_fixed_subagent_for_task(task["task_type"])
        response = find_latest_subagent_message(
            state.get("team_messages", []),
            task_id=task["task_id"],
            agent_id=definition.agent_id,
        )
        if response is None:
            raise TeamProtocolError("合并 Subagent 产物前缺少 result 消息")
        validated = validate_protocol_message(
            response,
            team=state["team"],
            allowed_artifact_refs=request["artifact_refs"],
        )
        if validated["message_type"] != "result":
            raise TeamProtocolError("只有 result 消息可以合并 Subagent 产物")
        if validated["summary"] != output.summary:
            raise TeamProtocolError("合并时发现摘要与结构化结果不一致")
        if validated["artifact_refs"] != output.artifact_refs:
            raise TeamProtocolError("合并时发现产物引用与结构化结果不一致")
        team = update_team_dispatch_status(
            state["team"],
            agent_id=definition.agent_id,
            task_id=task["task_id"],
            active=False,
        )
        return {
            "dispatch_result": output.model_copy(deep=True),
            "team": team,
            "team_messages": [validated],
        }
    except (KeyError, TypeError, ValueError) as error:
        return {
            "errors": [create_dispatch_error("merge_subagent_artifacts", error, fatal=True)]
        }


def append_task_output_refs(state: TeamOrchestrationGraphState) -> dict:
    """把合法 Subagent 产物引用登记到对应 Task 并消费分派请求。

    Args:
        state: 已完成摘要和引用合并的 Team Orchestration 状态。

    Returns:
        只更新 output_refs 和 updated_at 的 Task 局部记录，以及已清空的请求。
    """
    try:
        request = state["dispatch_request"]
        output = state.get("dispatch_result")
        if request is None or output is None:
            raise TeamProtocolError("登记 Task 产物前缺少请求或结果")
        task = resolve_subagent_task(state.get("tasks", []), request["task_id"])
        updated_task = dict(task)
        updated_task["output_refs"] = merge_task_output_refs(
            task.get("output_refs", []),
            output.artifact_refs,
        )
        updated_task["updated_at"] = utc_now_iso()
        return {
            "tasks": [cast(TaskItem, updated_task)],
            "dispatch_request": None,
        }
    except (KeyError, TypeError, ValueError) as error:
        return {
            "dispatch_request": None,
            "errors": [create_dispatch_error("append_task_output_refs", error, fatal=True)],
        }


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
