from __future__ import annotations

from typing import Literal

from langgraph.types import Send

from app.services.recovery_execution import (
    RECOVERY_RESUME_AFTER_NODES,
    RECOVERY_RETRY_NODES,
)
from app.services.task_system import TASK_DAG_TEMPLATE, build_task_id, validate_task_dag
from app.state.models import (
    ContentSubagentGraphState,
    ContextCompactGraphState,
    EvidenceGraphState,
    EvidenceSubagentGraphState,
    FileGovernanceState,
    InventoryGraphState,
    RecoveryGraphState,
    TeamOrchestrationGraphState,
    VersionAnalysisGraphState,
    VersionSubagentGraphState,
)

"""本模块实现各 LangGraph 通过 conditional_edge 明确调用的条件路由函数。"""


def route_context_compaction(
    state: ContextCompactGraphState,
) -> Literal["compact", "skip"]:
    """根据 Token 估算计划选择执行压缩或保持当前上下文。

    Args:
        state: 已执行 ``estimate_context_tokens`` 的 Context Compact 子图状态。

    Returns:
        计划明确要求压缩时返回 ``compact``，否则返回 ``skip``。
    """
    plan = state.get("plan")
    return "compact" if plan is not None and plan.get("should_compact") is True else "skip"


def route_recovery_reuse_result(
    state: RecoveryGraphState,
) -> Literal["reused", "decide", "abort"]:
    """根据成功节点执行检查结果选择复用、策略判断或安全终止。

    Args:
        state: 已执行 ``inspect_reusable_execution`` 的恢复子图状态。

    Returns:
        已找到可复用结果时返回 ``reused``；目标非法时返回 ``abort``；
        其余情况返回 ``decide``。
    """
    action = state["recovery"].get("action")
    if action == "reuse_result":
        return "reused"
    if action == "abort":
        return "abort"
    return "decide"


def route_recovery_action(
    state: RecoveryGraphState,
) -> Literal["retry", "fallback", "wait_human", "abort"]:
    """把确定性恢复策略动作路由到对应恢复节点。

    Args:
        state: 已执行 ``decide_recovery_action`` 的恢复子图状态。

    Returns:
        retry、fallback、wait_human 或 abort 中的固定分支。
    """
    action = state["recovery"].get("action")
    if action in {"retry", "fallback", "wait_human"}:
        return action
    return "abort"


def route_recovery_human_action(
    state: RecoveryGraphState,
) -> Literal["retry", "fallback", "abort"]:
    """根据恢复型人工输入选择重试、安全跳过或终止。

    Args:
        state: 已校验并应用人工恢复值的恢复子图状态。

    Returns:
        retry、fallback 或 abort 中的固定分支。
    """
    action = state["recovery"]["human"].get("selected_action")
    if action in {"retry", "provide_path"}:
        return "retry"
    if action == "skip_file":
        return "fallback"
    return "abort"


def resume_failed_stage(state: FileGovernanceState) -> str:
    """在 Error Recovery 结束后选择重试节点或第二段续跑路由。

    Args:
        state: 已合并第七个子图输出的顶层治理状态。

    Returns:
        白名单内的失败节点、续跑选择器或失败报告节点。
    """
    recovery = state.get("recovery", {})
    action = recovery.get("action")
    retry_node = recovery.get("resume_node")
    if action == "retry" and retry_node in RECOVERY_RETRY_NODES:
        return str(retry_node)
    if action in {
        "reuse_result",
        "skip_file",
        "fallback",
        "continue_partial",
    }:
        return "select_resume_after_failed_stage"
    if action == "abort" and retry_node == "execute_after_run_hooks":
        return "generate_lifecycle_failure_report"
    return "generate_failure_report"


def resume_after_failed_stage(state: FileGovernanceState) -> str:
    """在结果复用或安全降级后选择固定业务后继节点。

    Args:
        state: 已通过续跑选择器建立稳定 checkpoint 的顶层状态。

    Returns:
        白名单内的正常后继节点；目标缺失或非法时返回失败报告。
    """
    recovery = state.get("recovery", {})
    resume_after_node = recovery.get("resume_after_node")
    if (
        recovery.get("action") in {"reuse_result", "skip_file", "fallback", "continue_partial"}
        and resume_after_node in RECOVERY_RESUME_AFTER_NODES
    ):
        return str(resume_after_node)
    return "generate_failure_report"


def route_before_run_hooks_result(
    state: FileGovernanceState,
) -> Literal["continue", "failure"]:
    """根据 before_run 阶段的阻断错误选择请求校验或失败报告。

    Args:
        state: 已执行 before_run Hooks 的顶层治理状态。

    Returns:
        存在 Hook 阻断或基础设施错误时返回 ``failure``，否则返回 ``continue``。
    """
    has_blocking_error = any(
        error["fatal"]
        and error["category"] == "hook"
        and error["stage"] in {"before_run", "before_run_hooks"}
        for error in state.get("errors", [])
    )
    return "failure" if has_blocking_error else "continue"


def route_system_prompt_result(
    state: FileGovernanceState,
) -> Literal["continue", "failure"]:
    """根据 Prompt 加载状态选择 Inventory 子图或失败报告。

    Args:
        state: 已执行 System Prompt 加载节点的顶层治理状态。

    Returns:
        Prompt 已加载或已关闭时返回 ``continue``，其他状态返回 ``failure``。
    """
    prompt_status = state.get("prompt", {}).get("status")
    if prompt_status not in {"loaded", "disabled"}:
        return "failure"
    has_prompt_error = any(
        error["fatal"] and error["category"] == "prompt" for error in state.get("errors", [])
    )
    return "failure" if has_prompt_error else "continue"


def is_request_valid(state: FileGovernanceState) -> Literal["valid", "invalid"]:
    """根据请求校验阶段产生的致命错误选择继续或失败报告。"""
    has_validation_error = any(
        error["fatal"] and error["stage"] == "request_validation"
        for error in state.get("errors", [])
    )
    return "invalid" if has_validation_error else "valid"


def has_analyzable_documents(
    state: FileGovernanceState,
) -> Literal["analyzable", "empty", "failure"]:
    """在 Inventory 子图结束后区分可分析、无数据和致命失败。"""
    if any(error["fatal"] for error in state.get("errors", [])):
        return "failure"
    parsed_file_ids = {
        file_record["id"]
        for file_record in state.get("files", [])
        if file_record["parse_status"] == "parsed"
    }
    has_document = any(
        document["file_id"] in parsed_file_ids for document in state.get("documents", [])
    )
    return "analyzable" if has_document else "empty"


def has_pending_human_review(
    state: FileGovernanceState,
) -> Literal["review", "complete", "failure"]:
    """在 Recommendation 子图结束后选择失败、人工确认或直接报告。

    Args:
        state: 已完成独立 Recommendation 子图的顶层治理状态。

    Returns:
        存在致命错误时返回 ``failure``；存在待审核推荐时返回 ``review``；
        其余情况返回 ``complete``。
    """
    if any(error["fatal"] for error in state.get("errors", [])):
        return "failure"
    if any(decision["needs_human_review"] for decision in state.get("decisions", [])):
        return "review"
    return "complete"


def route_version_analysis_result(state: FileGovernanceState) -> Literal["success", "failure"]:
    """根据 Version Analysis 执行后的致命错误决定是否进入 Evidence。

    Args:
        state: 已合并版本组、差异、版本关系、分叉和版本链的顶层状态。

    Returns:
        没有致命错误时返回 ``success``，否则返回 ``failure``。
    """
    return "failure" if any(error["fatal"] for error in state.get("errors", [])) else "success"


def route_evidence_result(state: FileGovernanceState) -> Literal["success", "failure"]:
    """根据 Evidence 执行后的致命错误决定是否进入 Recommendation。

    发送日志缺失、不可读或单个 PDF 匹配失败属于可降级错误，不会阻断推荐；
    只有状态引用和证据关系不一致等致命错误才进入失败报告。

    Args:
        state: 已合并 PDF 来源、发送记录及 Evidence 错误的顶层状态。

    Returns:
        没有致命错误时返回 ``success``，否则返回 ``failure``。
    """
    return "failure" if any(error["fatal"] for error in state.get("errors", [])) else "success"


def route_team_orchestration_result(state: FileGovernanceState) -> Literal["success", "failure"]:
    """根据 Task 规划或状态同步结果决定继续业务流程还是生成失败报告。

    Args:
        state: 已执行顶层 Task 适配节点的文件治理状态。

    Returns:
        存在 Team Orchestration 致命错误时返回 ``failure``，否则返回 ``success``。
    """
    has_orchestration_error = any(
        error.get("stage") == "team_orchestration" and error.get("fatal") is True
        for error in state.get("errors", [])
    )
    return "failure" if has_orchestration_error else "success"


def route_skill_registry_result(
    state: FileGovernanceState,
) -> Literal["ready", "failure"]:
    """根据顶层 Skill 元数据加载结果选择 Task 规划或失败报告。

    Args:
        state: 已执行 ``load_skill_registry`` 节点的顶层治理状态。

    Returns:
        注册表 ready 且没有对应致命错误时返回 ``ready``，否则返回 ``failure``。
    """
    registry_ready = state.get("skill_registry", {}).get("status") == "ready"
    has_skill_error = any(
        error.get("stage") == "skills" and error.get("fatal") is True
        for error in state.get("errors", [])
    )
    return "ready" if registry_ready and not has_skill_error else "failure"


def route_skill_preparation_result(
    state: TeamOrchestrationGraphState,
) -> Literal["ready", "fallback"]:
    """根据 Skill 选择、加载和绑定结果决定调用 Subagent 或协调者回退。

    Args:
        state: 已依次执行三个 Skill 准备节点的 Team Orchestration 状态。

    Returns:
        当前选择、指令上下文和绑定均完整时返回 ``ready``，否则返回 ``fallback``。
    """
    skill_nodes = {
        "select_task_skills",
        "load_task_skills",
        "bind_task_skills",
    }
    has_error = any(error.get("node_name") in skill_nodes for error in state.get("errors", []))
    selection = state.get("skill_selection")
    context = state.get("skill_context", [])
    if has_error or selection is None or not context:
        return "fallback"
    selected_ids = selection.get("skill_ids", [])
    context_ids = [instruction.get("skill_id") for instruction in context]
    return "ready" if context_ids == selected_ids else "fallback"


def route_failure_report_task_sync(
    state: FileGovernanceState,
) -> Literal["sync", "skip"]:
    """判断失败报告是否具有可安全收口的 Task DAG。

    请求校验、Prompt 或 Task 规划阶段可能在固定 DAG 创建前失败，此时失败报告
    直接进入 after-run hooks。业务子图失败时 DAG 已合法创建，报告则继续同步
    Report Task，避免在主图中复制 ``generate_failure_report`` 节点。

    Args:
        state: 已生成失败报告的顶层文件治理状态。

    Returns:
        DAG 完整且没有编排致命错误时返回 ``sync``，否则返回 ``skip``。
    """
    if any(
        error.get("stage") == "team_orchestration" and error.get("fatal") is True
        for error in state.get("errors", [])
    ):
        return "skip"
    try:
        tasks = state.get("tasks", [])
        validate_task_dag(tasks)
        run_id = state["run"]["run_id"]
        expected_ids = {
            build_task_id(run_id, definition["task_type"]) for definition in TASK_DAG_TEMPLATE
        }
        actual_ids = {task["task_id"] for task in tasks}
        if len(tasks) != len(expected_ids) or actual_ids != expected_ids:
            return "skip"
        if any(task["task_id"] != build_task_id(run_id, task["task_type"]) for task in tasks):
            return "skip"
    except (KeyError, TypeError, ValueError):
        return "skip"
    return "sync"


def has_pending_parse_jobs(state: InventoryGraphState) -> Literal["pending", "done"]:
    """根据解析队列是否为空决定继续逐文件循环或结束 Inventory 子图。"""
    return "pending" if state.get("parse_queue") else "done"


def route_parser(
    state: InventoryGraphState,
) -> Literal["xlsx", "docx", "pdf", "unsupported"]:
    """根据当前文件的小写扩展名选择确定的只读解析节点。"""
    current_file_id = state.get("current_file_id")
    file_record = next(
        (item for item in state.get("files", []) if item["id"] == current_file_id),
        None,
    )
    if file_record is None:
        return "unsupported"
    extension = file_record["extension"]
    if extension in {".xlsx", ".docx", ".pdf"}:
        return extension[1:]
    return "unsupported"


def route_after_run_hooks_result(
    state: FileGovernanceState,
) -> Literal["finalize", "failure"]:
    """根据 after_run 阶段新增的阻断错误选择最终收口或生命周期失败报告。

    路由只检查 after_run 自身错误，避免把业务阶段已有的致命错误再次误判为
    生命周期失败；忽略策略产生的失败事件不会创建致命错误，因而正常收口。

    Args:
        state: 已执行 after_run Hooks 且已包含业务报告的顶层治理状态。

    Returns:
        after_run 阶段被阻断时返回 ``failure``，否则返回 ``finalize``。
    """
    has_blocking_error = any(
        error["fatal"]
        and error["category"] == "hook"
        and error["stage"] in {"after_run", "after_run_hooks"}
        for error in state.get("errors", [])
    )
    return "failure" if has_blocking_error else "finalize"


def parse_succeeded(state: InventoryGraphState) -> Literal["success", "failure"]:
    """根据当前标准化文档和错误字段判断文件解析是否成功。"""
    return (
        "success"
        if state.get("current_document") is not None and state.get("current_parse_error") is None
        else "failure"
    )


def has_pending_comparisons(
    state: VersionAnalysisGraphState,
) -> Literal["pending", "done"]:
    """根据比较队列是否为空决定继续文件对循环或开始构建版本图。"""
    return "pending" if state.get("comparison_queue") else "done"


def comparison_succeeded(
    state: VersionAnalysisGraphState,
) -> Literal["success", "failure"]:
    """根据当前差异草稿和错误字段判断文件对比较是否成功。"""
    return (
        "success"
        if state.get("current_diff") is not None and state.get("current_comparison_error") is None
        else "failure"
    )


def has_valid_version_subagent_summary(
    state: VersionAnalysisGraphState,
) -> Literal["apply", "deterministic", "comparison_failure"]:
    """在成功模型摘要、确定性回退和比较失败之间选择后续路径。

    只有结构化输出存在，且对应 Version Subagent 审计状态为 ``success``、没有
    使用协调者回退时才允许替换摘要。模型超时、缺少密钥、非法输出和协议回退
    均保留确定性摘要，不改变版本方向、相似度、关键修改或置信度。

    Args:
        state: 已完成可选 Version Subagent 编排调用的版本分析状态。

    Returns:
        比较本身失败时返回 ``comparison_failure``；成功模型输出返回 ``apply``；
        其余可降级情况返回 ``deterministic``。
    """
    if state.get("current_diff") is None or state.get("current_comparison_error"):
        return "comparison_failure"
    output = state.get("current_version_subagent_output")
    request = state.get("current_version_subagent_input")
    if output is None or request is None:
        return "deterministic"
    matching_calls = [
        call
        for call in state.get("llm_calls", [])
        if call.get("task_id") == request["task_id"] and call.get("agent_id") == "version-subagent"
    ]
    if not matching_calls:
        return "deterministic"
    latest_call = matching_calls[-1]
    if latest_call.get("status") != "success" or latest_call.get("fallback_used"):
        return "deterministic"
    return "apply"


def has_pdf_match_jobs(
    state: EvidenceGraphState,
) -> Literal["pdf_match", "done"]:
    """判断 Evidence 子图是否需要进入 PDF 并行匹配阶段。

    Args:
        state: 已创建 PDF 匹配任务的 Evidence 子图状态。

    Returns:
        存在待处理任务时返回 ``pdf_match``，否则返回 ``done``。
    """
    has_jobs = any(job["status"] == "pending" for job in state.get("pdf_match_jobs", []))
    return "pdf_match" if has_jobs else "done"


def dispatch_pdf_match_jobs(state: EvidenceGraphState) -> list[Send]:
    """使用 LangGraph Send 为每个运行中 PDF 任务创建隔离 Worker 状态。

    Args:
        state: 已由 fan-out 节点把待处理任务标记为运行中的子图状态。

    Returns:
        指向 ``match_pdf_to_source_version`` 节点的 Send 指令列表。
    """
    return [
        Send(
            "match_pdf_to_source_version",
            {
                "request": dict(state["request"]),
                "job": dict(job),
                "files": list(state.get("files", [])),
                "documents": list(state.get("documents", [])),
                "pdf_match_jobs": [],
                "pdf_exports": [],
                "errors": [],
            },
        )
        for job in state.get("pdf_match_jobs", [])
        if job["status"] == "running"
    ]


def route_task_dag_validation(
    state: TeamOrchestrationGraphState,
) -> Literal["valid", "invalid"]:
    """根据 Task 创建和 DAG 校验错误决定是否继续团队编排。

    Args:
        state: 已执行 Task 创建与 DAG 校验节点的团队编排状态。

    Returns:
        存在当前编排阶段致命错误时返回 ``invalid``，否则返回 ``valid``。
    """
    blocking_nodes = {"create_task_dag", "validate_task_dag"}
    has_blocking_error = any(
        error.get("stage") == "team_orchestration"
        and error.get("node_name") in blocking_nodes
        and error.get("fatal") is True
        for error in state.get("errors", [])
    )
    return "invalid" if has_blocking_error else "valid"


def route_team_initialization_result(
    state: TeamOrchestrationGraphState,
) -> Literal["valid", "invalid"]:
    """根据固定团队初始化结果决定是否继续实际角色分配。

    Args:
        state: 已执行固定团队初始化节点的 Team Orchestration 状态。

    Returns:
        团队合法时返回 ``valid``；初始化产生致命错误时返回 ``invalid``。
    """
    has_error = any(
        error.get("node_name") == "initialize_fixed_agent_team" and error.get("fatal") is True
        for error in state.get("errors", [])
    )
    return "invalid" if has_error else "valid"


def route_orchestration_action(
    state: TeamOrchestrationGraphState,
) -> Literal["status_sync", "dispatch", "invalid"]:
    """在 Task 状态同步、固定 Subagent 分派和非法命令之间选择路径。

    Args:
        state: 已完成团队初始化、角色分配和命令互斥校验的编排状态。

    Returns:
        无分派请求时返回 ``status_sync``，有请求时返回 ``dispatch``，
        当前编排准备阶段存在致命错误时返回 ``invalid``。
    """
    blocking_nodes = {"assign_tasks_to_roles", "validate_orchestration_action"}
    if any(
        error.get("node_name") in blocking_nodes and error.get("fatal") is True
        for error in state.get("errors", [])
    ):
        return "invalid"
    return "dispatch" if state.get("dispatch_request") is not None else "status_sync"


def route_subagent_payload_validation(
    state: TeamOrchestrationGraphState,
) -> Literal["assign", "fallback"]:
    """根据最小输入、真实 Task 和固定角色校验结果选择分派或协调者回退。

    Args:
        state: 已执行 Subagent 分派载荷校验节点的编排状态。

    Returns:
        当前载荷合法时返回 ``assign``，存在校验错误时返回 ``fallback``。
    """
    has_error = any(
        error.get("node_name") == "validate_subagent_payload" for error in state.get("errors", [])
    )
    return "fallback" if has_error else "assign"


def select_subagent(
    state: TeamOrchestrationGraphState,
) -> Literal["content", "version", "evidence", "fallback"]:
    """根据已验证 dispatch_request 的辨识字段选择唯一固定 Subagent。

    Args:
        state: 已创建 assignment Team Message 的编排状态。

    Returns:
        返回 ``content``、``version`` 或 ``evidence``；请求或 assignment
        不完整时返回 ``fallback``。
    """
    if any(
        error.get("node_name") == "create_assignment_message" for error in state.get("errors", [])
    ):
        return "fallback"
    request = state.get("dispatch_request")
    if not isinstance(request, dict):
        return "fallback"
    if "document_id" in request:
        return "content"
    if "comparison_id" in request:
        return "version"
    if "group_id" in request:
        return "evidence"
    return "fallback"


def route_team_message_validation(
    state: TeamOrchestrationGraphState,
) -> Literal["merge", "fallback"]:
    """根据 Subagent 返回消息与结构化结果的一致性选择合并或协调者回退。

    Args:
        state: 已执行 Team Message 校验节点的编排状态。

    Returns:
        当前响应合法且具有结构化结果时返回 ``merge``，否则返回 ``fallback``。
    """
    has_error = any(
        error.get("node_name") == "validate_team_message" for error in state.get("errors", [])
    )
    if has_error or state.get("dispatch_result") is None:
        return "fallback"
    return "merge"


def route_subagent_input_validation(
    state: ContentSubagentGraphState | VersionSubagentGraphState | EvidenceSubagentGraphState,
) -> Literal["valid", "invalid"]:
    """根据三个固定 Subagent 的输入协议校验结果选择 Prompt 或错误消息。

    Args:
        state: 已执行角色专属输入校验节点的 Subagent 子图状态。

    Returns:
        当前输入校验节点产生错误时返回 ``invalid``，否则返回 ``valid``。
    """
    input_nodes = {
        "validate_content_subagent_input",
        "validate_version_subagent_input",
        "validate_evidence_subagent_input",
    }
    has_input_error = any(
        error.get("node_name") in input_nodes for error in state.get("errors", [])
    )
    return "invalid" if has_input_error else "valid"


def route_subagent_prompt_validation(
    state: ContentSubagentGraphState | VersionSubagentGraphState | EvidenceSubagentGraphState,
) -> Literal["invoke", "error"]:
    """根据最小 Prompt 和固定 before-model 安全检查结果决定是否调用模型。

    Args:
        state: 已生成 Prompt 并执行固定 before-model 安全检查的子图状态。

    Returns:
        Prompt 构造或安全检查失败时返回 ``error``，否则返回 ``invoke``。
    """
    prompt_nodes = {
        "resolve_model_profile",
        "build_content_subagent_prompt",
        "build_version_subagent_prompt",
        "build_evidence_subagent_prompt",
        "execute_before_model_hooks",
    }
    has_prompt_error = any(
        error.get("node_name") in prompt_nodes for error in state.get("errors", [])
    )
    return "error" if has_prompt_error else "invoke"


def route_subagent_llm_result(
    state: ContentSubagentGraphState | VersionSubagentGraphState | EvidenceSubagentGraphState,
) -> Literal["validate", "fallback", "error"]:
    """根据结构化模型结果和回退开关选择输出校验、确定性回退或错误消息。

    Args:
        state: 已调用统一 LLM Client 并执行 after-model 安全检查的子图状态。

    Returns:
        有输出时返回 ``validate``；无输出且允许回退时返回 ``fallback``；
        其他情况返回 ``error``。
    """
    if state.get("output") is not None:
        return "validate"
    if state.get("llm", {}).get("fallback_enabled") is True:
        return "fallback"
    return "error"


def route_subagent_output_validation(
    state: ContentSubagentGraphState | VersionSubagentGraphState | EvidenceSubagentGraphState,
) -> Literal["persist", "fallback", "error"]:
    """根据输出 Schema 和引用白名单校验结果选择固化、回退或错误消息。

    Args:
        state: 已执行角色专属输出校验节点的 Subagent 子图状态。

    Returns:
        输出合法时返回 ``persist``；输出被拒绝且允许回退时返回 ``fallback``；
        其他情况返回 ``error``。
    """
    if state.get("output") is not None:
        return "persist"
    if state.get("llm", {}).get("fallback_enabled") is True:
        return "fallback"
    return "error"
