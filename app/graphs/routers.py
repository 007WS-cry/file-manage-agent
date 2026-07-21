from __future__ import annotations

from typing import Literal

from langgraph.types import Send

from app.state.models import (
    EvidenceGraphState,
    FileGovernanceState,
    InventoryGraphState,
    TeamOrchestrationGraphState,
    VersionAnalysisGraphState,
)

"""本模块实现顶层治理图以及 Inventory、Version Analysis、Evidence 子图路由。"""


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
        error["fatal"] and error["category"] == "prompt"
        for error in state.get("errors", [])
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
        document["file_id"] in parsed_file_ids
        for document in state.get("documents", [])
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
        if state.get("current_document") is not None
        and state.get("current_parse_error") is None
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
        if state.get("current_diff") is not None
        and state.get("current_comparison_error") is None
        else "failure"
    )


def has_pdf_match_jobs(
    state: EvidenceGraphState,
) -> Literal["pdf_match", "done"]:
    """判断 Evidence 子图是否需要进入 PDF 并行匹配阶段。

    Args:
        state: 已创建 PDF 匹配任务的 Evidence 子图状态。

    Returns:
        存在待处理任务时返回 ``pdf_match``，否则返回 ``done``。
    """
    has_jobs = any(
        job["status"] == "pending" for job in state.get("pdf_match_jobs", [])
    )
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
