from __future__ import annotations

from typing import Literal

from app.services.context_compaction import (
    copy_context_compact_state,
    copy_document_record,
    copy_prompt_state,
)
from app.services.memory_policy import copy_memory_state
from app.skills.loader import create_pending_skill_registry
from app.skills.registry import copy_skill_registry
from app.state.factories import (
    copy_application_database_state,
    copy_recovery_state,
)
from app.state.models import (
    ContentSubagentInput,
    ContextCompactGraphState,
    EvidenceGraphState,
    EvidenceSubagentInput,
    FileGovernanceState,
    InventoryGraphState,
    RecommendationGraphState,
    RecoveryGraphState,
    TaskStatusUpdate,
    TeamOrchestrationGraphState,
    TeamState,
    VersionAnalysisGraphState,
    VersionSubagentInput,
    VersionSubagentOutput,
)

"""本模块转换顶层、业务及团队编排状态，并隔离队列和单次分派私有字段。"""


def _copy_team_state(team: TeamState) -> TeamState:
    """复制 Team 状态及其成员列表，避免子图修改顶层可变对象。

    Args:
        team: 顶层状态中的固定 Agent Team。

    Returns:
        成员字典和列表均与输入解除可变引用关系的 Team 状态。
    """
    return TeamState(
        coordinator_id=team["coordinator_id"],
        members=[dict(member) for member in team.get("members", [])],
        protocol_version=team["protocol_version"],
        max_parallel_agents=team["max_parallel_agents"],
    )


def file_governance_to_recovery_state(
    state: FileGovernanceState,
) -> RecoveryGraphState:
    """把顶层治理状态转换为隔离的 Error Recovery 子图输入。

    恢复子图只接收运行、路径配置、应用数据库、Task、错误、节点执行和降级记录。
    文件正文、版本比较、推荐、模型消息和最终报告不会进入恢复判断。

    Args:
        state: 已通过条件边或子图异常处理入口转入恢复流程的顶层状态。

    Returns:
        可独立 checkpoint 且不包含数据库 Session 的恢复子图状态。
    """
    return RecoveryGraphState(
        run=dict(state["run"]),
        request=dict(state["request"]),
        workspace=dict(state["workspace"]),
        application_database=copy_application_database_state(state.get("application_database")),
        tasks=[dict(task) for task in state.get("tasks", [])],
        errors=[dict(error) for error in state.get("errors", [])],
        node_executions=[
            {
                **dict(execution),
                "result_refs": list(execution.get("result_refs", [])),
            }
            for execution in state.get("node_executions", [])
        ],
        degradations=[
            {
                **dict(degradation),
                "affected_file_ids": list(degradation.get("affected_file_ids", [])),
            }
            for degradation in state.get("degradations", [])
        ],
        recovery=copy_recovery_state(state.get("recovery")),
    )


def recovery_state_to_file_governance_update(
    state: RecoveryGraphState,
) -> dict:
    """把 Error Recovery 结果过滤为允许写回顶层的字段。

    Args:
        state: 已完成复用、重试安排、安全降级、人工选择或终止判断的恢复状态。

    Returns:
        运行、可选路径修正、恢复审计及 Task 状态组成的白名单更新。
    """
    return {
        "run": dict(state["run"]),
        "request": dict(state["request"]),
        "workspace": dict(state["workspace"]),
        "application_database": copy_application_database_state(state.get("application_database")),
        "tasks": [dict(task) for task in state.get("tasks", [])],
        "errors": [dict(error) for error in state.get("errors", [])],
        "node_executions": [
            {
                **dict(execution),
                "result_refs": list(execution.get("result_refs", [])),
            }
            for execution in state.get("node_executions", [])
        ],
        "degradations": [
            {
                **dict(degradation),
                "affected_file_ids": list(degradation.get("affected_file_ids", [])),
            }
            for degradation in state.get("degradations", [])
        ],
        "recovery": copy_recovery_state(state.get("recovery")),
    }


def file_governance_to_context_compact_state(
    state: FileGovernanceState,
    *,
    stage: Literal["after_inventory", "after_evidence"],
) -> ContextCompactGraphState:
    """把顶层治理状态转换为一次隔离的 Context Compact 子图输入。

    Args:
        state: 已完成 Inventory 或 Evidence 阶段的顶层治理状态。
        stage: 本次固定压缩阶段。

    Returns:
        只包含运行、工作空间、Prompt、文档和压缩私有字段的子图状态。
    """
    return ContextCompactGraphState(
        run=dict(state["run"]),
        workspace=dict(state["workspace"]),
        prompt=copy_prompt_state(state["prompt"]),
        documents=[copy_document_record(document) for document in state.get("documents", [])],
        context_compact=copy_context_compact_state(state.get("context_compact")),
        stage=stage,
        plan=None,
        compaction_payload=None,
        summary_draft=None,
        errors=[dict(error) for error in state.get("errors", [])],
    )


def context_compact_state_to_file_governance_update(
    state: ContextCompactGraphState,
) -> dict:
    """把 Context Compact 结果过滤为允许写回顶层的字段。

    压缩计划、未跟踪临时载荷和摘要草稿均属于子图私有字段，不会进入顶层
    checkpoint。业务版本事实、Evidence、推荐和人工选择不在返回白名单中。

    Args:
        state: 已完成压缩、跳过或安全降级的 Context Compact 子图状态。

    Returns:
        Prompt、文档、Context Compact 索引和结构化错误更新。
    """
    return {
        "prompt": copy_prompt_state(state["prompt"]),
        "documents": [copy_document_record(document) for document in state.get("documents", [])],
        "context_compact": copy_context_compact_state(state.get("context_compact")),
        "errors": [dict(error) for error in state.get("errors", [])],
    }


def file_governance_to_team_orchestration_state(
    state: FileGovernanceState,
    *,
    task_update: TaskStatusUpdate | None = None,
    dispatch_request: (
        ContentSubagentInput | VersionSubagentInput | EvidenceSubagentInput | None
    ) = None,
) -> TeamOrchestrationGraphState:
    """把顶层治理状态转换为 Team Orchestration 子图输入。

    顶层运行信息、Task 和 Todo 按值复制。``task_update`` 只作为本次子图调用的
    私有命令传入；顶层已有业务错误不进入子图，避免干扰 DAG 校验路由。

    Args:
        state: 包含运行信息和可选已有 Task、Todo 的顶层治理状态。
        task_update: 本次需要消费的可选 Task 状态更新命令。
        dispatch_request: 本次 Team Orchestration 调用使用的可选固定 Subagent 请求。

    Returns:
        已隔离顶层业务错误且包含独立数据副本的团队编排子图状态。
    """
    registry = state.get("skill_registry", create_pending_skill_registry())
    return TeamOrchestrationGraphState(
        run=dict(state["run"]),
        llm=dict(state["llm"]),
        team=_copy_team_state(state["team"]),
        skill_registry=copy_skill_registry(registry),
        skill_selection=None,
        skill_context=[],
        task_update=dict(task_update) if task_update is not None else None,
        dispatch_request=(dict(dispatch_request) if dispatch_request is not None else None),
        dispatch_result=None,
        tasks=[dict(task) for task in state.get("tasks", [])],
        todos=[dict(todo) for todo in state.get("todos", [])],
        team_messages=[dict(message) for message in state.get("team_messages", [])],
        llm_calls=[dict(call) for call in state.get("llm_calls", [])],
        errors=[],
    )


def team_orchestration_state_to_file_governance_update(
    state: TeamOrchestrationGraphState,
) -> dict:
    """把 Team Orchestration 结果过滤为允许写回顶层的字段。

    0.4.4 返回 Task、Todo、固定团队运行状态、协议消息、模型审计和新错误。
    ``task_update``、``dispatch_request`` 与 ``dispatch_result`` 仍是单次调用私有字段，
    不会进入顶层状态。

    Args:
        state: 已完成执行的 Team Orchestration 子图状态。

    Returns:
        可由顶层 reducer 合并且不包含私有分派载荷的字段白名单更新。
    """
    return {
        "team": _copy_team_state(state["team"]),
        "skill_registry": copy_skill_registry(state["skill_registry"]),
        "tasks": [dict(task) for task in state.get("tasks", [])],
        "todos": [dict(todo) for todo in state.get("todos", [])],
        "team_messages": [dict(message) for message in state.get("team_messages", [])],
        "llm_calls": [dict(call) for call in state.get("llm_calls", [])],
        "errors": [dict(error) for error in state.get("errors", [])],
    }


def file_governance_to_inventory_state(
    state: FileGovernanceState,
) -> InventoryGraphState:
    """把顶层治理状态转换为 Inventory 子图的完整输入状态。

    仅传入扫描与内容提取所需的请求、工作空间、文件、文档和错误字段。
    ``run``、人工审核、版本关系和报告等顶层字段不会进入 Inventory 子图；
    逐文件队列和当前解析结果在每次子图调用时显式初始化。

    Args:
        state: 顶层文件版本治理状态。

    Returns:
        所有 Inventory 私有字段均已初始化的子图状态。
    """
    return InventoryGraphState(
        request=dict(state["request"]),
        workspace=dict(state["workspace"]),
        discovered_paths=[],
        parse_queue=[],
        current_file_id=None,
        current_raw_content=None,
        current_document=None,
        current_parse_error=None,
        files=list(state.get("files", [])),
        documents=list(state.get("documents", [])),
        errors=list(state.get("errors", [])),
    )


def inventory_state_to_file_governance_update(
    state: InventoryGraphState,
) -> dict:
    """把 Inventory 子图结果转换为允许合并回顶层状态的更新。

    只返回跨阶段有价值的文件记录、标准化文档和错误。发现路径、解析队列、
    当前文件、原始解析内容和临时文档不会泄漏到 ``FileGovernanceState``。

    Args:
        state: 已完成执行的 Inventory 子图状态。

    Returns:
        可由顶层状态 reducer 安全合并的字段白名单更新。
    """
    return {
        "files": list(state.get("files", [])),
        "documents": list(state.get("documents", [])),
        "errors": list(state.get("errors", [])),
    }


def file_governance_to_version_analysis_state(
    state: FileGovernanceState,
) -> VersionAnalysisGraphState:
    """把顶层治理状态转换为 Version Analysis 子图的完整输入状态。

    文件、标准化文档、真实 Task DAG 和已有业务结果按值传入，比较任务队列、
    当前比较草稿和单次 Version 分派字段显式初始化。工作空间和最终报告不会
    传入版本分析子图。

    Args:
        state: 已完成 Inventory 阶段的顶层治理状态。

    Returns:
        所有版本分析私有字段均已初始化的子图状态。
    """
    return VersionAnalysisGraphState(
        run=dict(state["run"]),
        request=dict(state["request"]),
        llm=dict(state["llm"]),
        team=_copy_team_state(state["team"]),
        skill_registry=copy_skill_registry(
            state.get("skill_registry", create_pending_skill_registry())
        ),
        tasks=[dict(task) for task in state.get("tasks", [])],
        todos=[dict(todo) for todo in state.get("todos", [])],
        files=list(state.get("files", [])),
        documents=list(state.get("documents", [])),
        version_groups=list(state.get("version_groups", [])),
        comparison_jobs=[],
        comparison_queue=[],
        current_comparison_id=None,
        current_diff=None,
        current_version_subagent_input=None,
        current_version_subagent_output=None,
        current_comparison_error=None,
        diffs=list(state.get("diffs", [])),
        version_edges=list(state.get("version_edges", [])),
        branches=list(state.get("branches", [])),
        version_chains=list(state.get("version_chains", [])),
        decisions=[],
        human_review={
            "pending_group_ids": [],
            "selections": {},
            "review_note": state["human_review"].get("review_note"),
        },
        team_messages=[dict(message) for message in state.get("team_messages", [])],
        llm_calls=[dict(call) for call in state.get("llm_calls", [])],
        errors=list(state.get("errors", [])),
    )


def version_analysis_state_to_file_governance_update(
    state: VersionAnalysisGraphState,
) -> dict:
    """把 Version Analysis 子图结果转换为顶层治理状态更新。

    返回版本事实以及内部 Team Orchestration 更新的团队、Task、Todo、协议消息
    和模型审计。推荐与人工审核已经迁移到独立 Recommendation 子图；比较任务、
    队列、当前差异草稿及单次分派载荷不会合并回顶层状态。

    Args:
        state: 已完成执行的 Version Analysis 子图状态。

    Returns:
        可由顶层状态 reducer 安全合并的版本分析字段白名单更新。
    """
    return {
        "team": _copy_team_state(state["team"]),
        "skill_registry": copy_skill_registry(state["skill_registry"]),
        "tasks": [dict(task) for task in state.get("tasks", [])],
        "todos": [dict(todo) for todo in state.get("todos", [])],
        "version_groups": list(state.get("version_groups", [])),
        "diffs": list(state.get("diffs", [])),
        "version_edges": list(state.get("version_edges", [])),
        "branches": list(state.get("branches", [])),
        "version_chains": list(state.get("version_chains", [])),
        "team_messages": [dict(message) for message in state.get("team_messages", [])],
        "llm_calls": [dict(call) for call in state.get("llm_calls", [])],
        "errors": list(state.get("errors", [])),
    }


def version_analysis_to_team_orchestration_state(
    state: VersionAnalysisGraphState,
    dispatch_request: VersionSubagentInput,
) -> TeamOrchestrationGraphState:
    """把当前版本比较状态转换为一次 Version Subagent 编排输入。

    Args:
        state: 已生成确定性差异和最小 Version 输入的版本分析状态。
        dispatch_request: 已准备且不含完整正文的 Version Subagent 输入。

    Returns:
        包含真实 Task DAG、固定团队和审计历史的独立编排子图状态。
    """
    return TeamOrchestrationGraphState(
        run=dict(state["run"]),
        llm=dict(state["llm"]),
        team=_copy_team_state(state["team"]),
        skill_registry=copy_skill_registry(state["skill_registry"]),
        skill_selection=None,
        skill_context=[],
        task_update=None,
        dispatch_request=dict(dispatch_request),
        dispatch_result=None,
        tasks=[dict(task) for task in state.get("tasks", [])],
        todos=[dict(todo) for todo in state.get("todos", [])],
        team_messages=[dict(message) for message in state.get("team_messages", [])],
        llm_calls=[dict(call) for call in state.get("llm_calls", [])],
        errors=[],
    )


def team_orchestration_state_to_version_analysis_update(
    state: TeamOrchestrationGraphState,
) -> dict:
    """把一次 Version 分派结果过滤为版本分析子图允许写回的字段。

    Args:
        state: 已完成 Version Subagent 调用、协议校验或协调者回退的编排状态。

    Returns:
        团队、Task、Todo、结构化输出、消息、审计和新增错误的字段白名单更新。
    """
    raw_output = state.get("dispatch_result")
    output = raw_output if isinstance(raw_output, VersionSubagentOutput) else None
    return {
        "team": _copy_team_state(state["team"]),
        "skill_registry": copy_skill_registry(state["skill_registry"]),
        "tasks": [dict(task) for task in state.get("tasks", [])],
        "todos": [dict(todo) for todo in state.get("todos", [])],
        "current_version_subagent_output": (
            output.model_copy(deep=True) if output is not None else None
        ),
        "team_messages": [dict(message) for message in state.get("team_messages", [])],
        "llm_calls": [dict(call) for call in state.get("llm_calls", [])],
        "errors": [dict(error) for error in state.get("errors", [])],
    }


def file_governance_to_evidence_state(
    state: FileGovernanceState,
) -> EvidenceGraphState:
    """把顶层治理状态转换为 Evidence 子图的完整输入状态。

    请求、文件、标准化文档、版本组及已有证据按值传入；PDF 候选、匹配任务和
    原始发送日志属于单次子图调用的私有状态，每次调用时显式初始化为空。

    Args:
        state: 已完成 Inventory 和 Version Analysis 阶段的顶层治理状态。

    Returns:
        所有 Evidence 私有执行字段均已初始化的子图状态。
    """
    return EvidenceGraphState(
        run=dict(state["run"]),
        request=dict(state["request"]),
        files=list(state.get("files", [])),
        documents=list(state.get("documents", [])),
        version_groups=list(state.get("version_groups", [])),
        memory=copy_memory_state(state.get("memory")),
        pdf_candidate_ids=[],
        pdf_match_jobs=[],
        delivery_log_entries=[],
        pdf_exports=list(state.get("pdf_exports", [])),
        deliveries=list(state.get("deliveries", [])),
        errors=list(state.get("errors", [])),
    )


def evidence_state_to_file_governance_update(
    state: EvidenceGraphState,
) -> dict:
    """把 Evidence 子图结果转换为允许合并回顶层状态的更新。

    只返回 PDF 来源、发送证据和结构化错误。PDF 候选 ID、匹配任务以及原始
    本地日志记录不会进入顶层状态，避免扩大 checkpoint 和后续 LLM 上下文。

    Args:
        state: 已完成执行的 Evidence 子图状态。

    Returns:
        可由顶层 reducer 安全合并的证据字段白名单更新。
    """
    return {
        "memory": copy_memory_state(state.get("memory")),
        "pdf_exports": list(state.get("pdf_exports", [])),
        "deliveries": list(state.get("deliveries", [])),
        "errors": list(state.get("errors", [])),
    }


def file_governance_to_recommendation_state(
    state: FileGovernanceState,
) -> RecommendationGraphState:
    """把顶层治理状态转换为 Recommendation 子图的完整输入状态。

    文件事实、版本关系和外部证据按值传入；候选集合与推荐记录在每次调用时
    重新建立，避免沿用 Version Analysis 阶段的临时候选或上次执行结果。

    Args:
        state: 已完成版本分析和 Evidence 阶段的顶层治理状态。

    Returns:
        候选集合和推荐结果均已清空的 Recommendation 子图状态。
    """
    return RecommendationGraphState(
        run=dict(state["run"]),
        request=dict(state["request"]),
        files=list(state.get("files", [])),
        version_groups=list(state.get("version_groups", [])),
        diffs=list(state.get("diffs", [])),
        version_edges=list(state.get("version_edges", [])),
        branches=list(state.get("branches", [])),
        version_chains=list(state.get("version_chains", [])),
        pdf_exports=list(state.get("pdf_exports", [])),
        deliveries=list(state.get("deliveries", [])),
        memory=copy_memory_state(state.get("memory")),
        candidate_sets=[],
        decisions=[],
        human_review={
            "pending_group_ids": [],
            "selections": {},
            "review_note": state["human_review"].get("review_note"),
        },
        errors=list(state.get("errors", [])),
    )


def recommendation_state_to_file_governance_update(
    state: RecommendationGraphState,
) -> dict:
    """把 Recommendation 子图结果转换为允许合并回顶层状态的更新。

    只返回最终推荐、人工审核状态和结构化错误；候选集合是子图内部执行事实，
    不进入顶层 checkpoint，也不会扩大人工审核或后续报告的输入状态。

    Args:
        state: 已完成执行的 Recommendation 子图状态。

    Returns:
        可由顶层 reducer 安全合并的推荐字段白名单更新。
    """
    return {
        "memory": copy_memory_state(state.get("memory")),
        "decisions": list(state.get("decisions", [])),
        "human_review": dict(state["human_review"]),
        "errors": list(state.get("errors", [])),
    }
