from __future__ import annotations

from app.state.models import (
    EvidenceGraphState,
    FileGovernanceState,
    InventoryGraphState,
    RecommendationGraphState,
    TaskStatusUpdate,
    TeamOrchestrationGraphState,
    VersionAnalysisGraphState,
)

"""本模块显式转换顶层状态与五个子图状态，并隔离各子图私有执行字段。"""


def file_governance_to_team_orchestration_state(
    state: FileGovernanceState,
    *,
    task_update: TaskStatusUpdate | None = None,
) -> TeamOrchestrationGraphState:
    """把顶层治理状态转换为 Team Orchestration 子图输入。

    顶层运行信息、Task 和 Todo 按值复制。``task_update`` 只作为本次子图调用的
    私有命令传入；顶层已有业务错误不进入子图，避免干扰 DAG 校验路由。

    Args:
        state: 包含运行信息和可选已有 Task、Todo 的顶层治理状态。
        task_update: 本次需要消费的可选 Task 状态更新命令。

    Returns:
        已隔离顶层业务错误且包含独立数据副本的团队编排子图状态。
    """
    return TeamOrchestrationGraphState(
        run=dict(state["run"]),
        task_update=dict(task_update) if task_update is not None else None,
        tasks=[dict(task) for task in state.get("tasks", [])],
        todos=[dict(todo) for todo in state.get("todos", [])],
        errors=[],
    )


def team_orchestration_state_to_file_governance_update(
    state: TeamOrchestrationGraphState,
) -> dict:
    """把 Team Orchestration 结果过滤为允许写回顶层的字段。

    只返回 Task、Todo 和新产生的结构化错误。子图私有的 ``task_update`` 无论是否
    已被消费都不会进入 ``FileGovernanceState``，从状态转换边界防止命令泄漏。

    Args:
        state: 已完成执行的 Team Orchestration 子图状态。

    Returns:
        可由顶层 reducer 合并的 Task、Todo 和错误字段白名单更新。
    """
    return {
        "tasks": [dict(task) for task in state.get("tasks", [])],
        "todos": [dict(todo) for todo in state.get("todos", [])],
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

    文件、标准化文档和已有业务结果按值传入，比较任务队列与当前比较草稿
    显式初始化。运行生命周期、工作空间和最终报告不会传入版本分析子图。

    Args:
        state: 已完成 Inventory 阶段的顶层治理状态。

    Returns:
        所有版本分析私有字段均已初始化的子图状态。
    """
    return VersionAnalysisGraphState(
        request=dict(state["request"]),
        files=list(state.get("files", [])),
        documents=list(state.get("documents", [])),
        version_groups=list(state.get("version_groups", [])),
        comparison_jobs=[],
        comparison_queue=[],
        current_comparison_id=None,
        current_diff=None,
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
        errors=list(state.get("errors", [])),
    )


def version_analysis_state_to_file_governance_update(
    state: VersionAnalysisGraphState,
) -> dict:
    """把 Version Analysis 子图结果转换为顶层治理状态更新。

    只返回版本分组、差异、版本边、分叉、版本链和错误。推荐与人工审核已经
    迁移到独立 Recommendation 子图；比较任务、队列和当前差异草稿也不会
    合并回顶层状态。

    Args:
        state: 已完成执行的 Version Analysis 子图状态。

    Returns:
        可由顶层状态 reducer 安全合并的版本分析字段白名单更新。
    """
    return {
        "version_groups": list(state.get("version_groups", [])),
        "diffs": list(state.get("diffs", [])),
        "version_edges": list(state.get("version_edges", [])),
        "branches": list(state.get("branches", [])),
        "version_chains": list(state.get("version_chains", [])),
        "errors": list(state.get("errors", [])),
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
        request=dict(state["request"]),
        files=list(state.get("files", [])),
        documents=list(state.get("documents", [])),
        version_groups=list(state.get("version_groups", [])),
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
        request=dict(state["request"]),
        files=list(state.get("files", [])),
        version_groups=list(state.get("version_groups", [])),
        diffs=list(state.get("diffs", [])),
        version_edges=list(state.get("version_edges", [])),
        branches=list(state.get("branches", [])),
        version_chains=list(state.get("version_chains", [])),
        pdf_exports=list(state.get("pdf_exports", [])),
        deliveries=list(state.get("deliveries", [])),
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
        "decisions": list(state.get("decisions", [])),
        "human_review": dict(state["human_review"]),
        "errors": list(state.get("errors", [])),
    }
