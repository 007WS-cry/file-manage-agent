from __future__ import annotations

from app.state.models import (
    EvidenceGraphState,
    FileGovernanceState,
    InventoryGraphState,
    VersionAnalysisGraphState,
)

"""本模块显式转换顶层状态与三个子图状态，并隔离各子图私有执行字段。"""


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
        decisions=list(state.get("decisions", [])),
        human_review=dict(state["human_review"]),
        errors=list(state.get("errors", [])),
    )


def version_analysis_state_to_file_governance_update(
    state: VersionAnalysisGraphState,
) -> dict:
    """把 Version Analysis 子图结果转换为顶层治理状态更新。

    只返回版本分组、差异、版本边、分叉、版本链、推荐、人工审核和错误。
    比较任务、比较队列、当前任务和当前差异草稿均属于子图私有执行状态，
    不会合并回顶层状态。

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
        "decisions": list(state.get("decisions", [])),
        "human_review": dict(state["human_review"]),
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
