from __future__ import annotations

from app.state.models import FileGovernanceState
from app.utils.runtime import create_error_record
from app.utils.task_tracking import (
    apply_task_status,
    build_content_dispatch_requests,
    build_evidence_dispatch_requests,
    dispatch_stage_subagent_requests,
    has_orchestration_failure,
    public_task_update,
    settle_unfinished_tasks_before_report,
    sync_business_task_status,
)

"""本模块只定义顶层 Task 状态同步及 Content、Evidence 阶段分派的图节点。"""


def plan_run_tasks(state: FileGovernanceState) -> dict:
    """幂等创建固定 Task DAG、分配逻辑角色并启动 Inventory Task。

    Args:
        state: 已通过请求校验和 System Prompt 加载的顶层治理状态。

    Returns:
        六个固定 Task、四个 Todo 及可选编排错误。
    """
    working_state, errors = apply_task_status(state, "inventory", "running")
    return public_task_update(working_state, errors)


def sync_inventory_task_status(state: FileGovernanceState) -> dict:
    """同步 Inventory 结果，并在有可分析文档时启动 Version Analysis。

    Args:
        state: 已合并 Inventory 子图公开结果的顶层治理状态。

    Returns:
        Inventory 和可选后续 Task 状态、Todo 及编排错误。
    """
    return sync_business_task_status(
        state,
        "inventory",
        next_task_type="version_analysis",
    )


def dispatch_content_subagent_task(state: FileGovernanceState) -> dict:
    """在 Inventory 完成后为每个标准化文档分派 Content Subagent。

    分派只传递短内容预览、结构摘要、关键字段和标准化产物引用。单个模型、
    Pydantic 或协议失败由 Team Orchestration 生成协调者回退结果，不会阻断
    后续确定性 Version Analysis；只有编排基础契约的致命错误才由主图路由失败。

    Args:
        state: 已完成 Inventory Task 同步且已启动 Version Analysis Task 的顶层状态。

    Returns:
        固定团队、Task、Todo、Team Message、LLM 审计及新增错误的顶层更新。
    """
    try:
        requests = build_content_dispatch_requests(state)
        working_state, errors = dispatch_stage_subagent_requests(state, requests)
        return {
            "team": working_state["team"],
            "tasks": list(working_state.get("tasks", [])),
            "todos": list(working_state.get("todos", [])),
            "team_messages": list(working_state.get("team_messages", [])),
            "llm_calls": list(working_state.get("llm_calls", [])),
            "errors": errors,
        }
    except Exception as error:
        return {
            "errors": [
                create_error_record(
                    stage="content_subagent",
                    node_name="dispatch_content_subagent_task",
                    category="protocol",
                    message=(
                        f"{type(error).__name__}: Content 阶段分派未完成，"
                        "已保留 Inventory 确定性结果。"
                    ),
                    fatal=False,
                )
            ]
        }


def sync_version_task_status(state: FileGovernanceState) -> dict:
    """同步 Version Analysis 结果并在成功时启动 Evidence Task。

    Args:
        state: 已合并版本分析子图公开结果的顶层治理状态。

    Returns:
        Version Analysis 和 Evidence Task 状态、Todo 及编排错误。
    """
    return sync_business_task_status(
        state,
        "version_analysis",
        next_task_type="evidence",
    )


def sync_evidence_task_status(state: FileGovernanceState) -> dict:
    """同步 Evidence 结果并在无致命错误时启动 Recommendation Task。

    Args:
        state: 已合并证据子图公开结果的顶层治理状态。

    Returns:
        Evidence 和 Recommendation Task 状态、Todo 及编排错误。
    """
    return sync_business_task_status(
        state,
        "evidence",
        next_task_type="recommendation",
    )


def dispatch_evidence_subagent_task(state: FileGovernanceState) -> dict:
    """在 Evidence 完成后按版本组分派 Evidence Subagent 解释任务。

    输入只包含 PDF 来源匹配摘要、发送证据摘要和受控引用，不读取完整 PDF、
    邮件或业务正文。Subagent 结果只提供解释文本，不能修改确定性证据匹配或
    Recommendation 使用的评分事实。

    Args:
        state: 已完成 Evidence Task 同步且已启动 Recommendation Task 的顶层状态。

    Returns:
        固定团队、Task、Todo、Team Message、LLM 审计及新增错误的顶层更新。
    """
    try:
        requests = build_evidence_dispatch_requests(state)
        working_state, errors = dispatch_stage_subagent_requests(state, requests)
        return {
            "team": working_state["team"],
            "tasks": list(working_state.get("tasks", [])),
            "todos": list(working_state.get("todos", [])),
            "team_messages": list(working_state.get("team_messages", [])),
            "llm_calls": list(working_state.get("llm_calls", [])),
            "errors": errors,
        }
    except Exception as error:
        return {
            "errors": [
                create_error_record(
                    stage="evidence_subagent",
                    node_name="dispatch_evidence_subagent_task",
                    category="protocol",
                    message=(
                        f"{type(error).__name__}: Evidence 阶段分派未完成，"
                        "已保留确定性证据匹配结果。"
                    ),
                    fatal=False,
                )
            ]
        }


def sync_recommendation_task_status(state: FileGovernanceState) -> dict:
    """同步 Recommendation 结果并启动或正常跳过 Human Review Task。

    Args:
        state: 已合并推荐子图公开结果的顶层治理状态。

    Returns:
        Recommendation 和 Human Review Task 状态、Todo 及编排错误。
    """
    return sync_business_task_status(
        state,
        "recommendation",
        next_task_type=None,
    )


def sync_human_review_task_status(state: FileGovernanceState) -> dict:
    """在人工恢复选择应用后完成 Human Review Task。

    Args:
        state: 已应用人工选择并清空待审核组的顶层治理状态。

    Returns:
        Human Review Task 完成状态、Todo 及编排错误。
    """
    working_state, errors = apply_task_status(
        state,
        "human_review",
        "completed",
        output_refs=("decisions", "human_review"),
    )
    return public_task_update(working_state, errors)


def sync_report_task_status(state: FileGovernanceState) -> dict:
    """收口未执行的上游 Task，并把已生成的治理报告登记为完成。

    成功、无数据和业务失败报告均走同一节点。报告 Task 只描述报告是否生成，
    不继承业务 Task 的失败状态，因此下游阻断不会被误报为报告自身失败。

    Args:
        state: 已生成任一种业务报告且具有合法固定 Task DAG 的顶层状态。

    Returns:
        全部 Task 终态、最终 Todo 投影及可选编排错误。
    """
    working_state, errors = settle_unfinished_tasks_before_report(state)
    if has_orchestration_failure(errors):
        return public_task_update(working_state, errors)
    working_state, running_errors = apply_task_status(
        working_state,
        "report",
        "running",
    )
    errors.extend(running_errors)
    if has_orchestration_failure(running_errors):
        return public_task_update(working_state, errors)
    working_state, completed_errors = apply_task_status(
        working_state,
        "report",
        "completed",
        output_refs=("report",),
    )
    errors.extend(completed_errors)
    return public_task_update(working_state, errors)
