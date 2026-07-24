from __future__ import annotations

from app.graphs.context_compact import context_compact_graph
from app.graphs.error_recovery import error_recovery_graph
from app.graphs.evidence import evidence_graph
from app.graphs.inventory import inventory_graph
from app.graphs.recommendation import recommendation_graph
from app.graphs.version_analysis import version_analysis_graph
from app.services.recovery_execution import (
    execute_recoverable_subgraph,
    hydrate_recovery_graph_state,
    load_recovery_reused_update,
)
from app.state.converters import (
    context_compact_state_to_file_governance_update,
    evidence_state_to_file_governance_update,
    file_governance_to_context_compact_state,
    file_governance_to_evidence_state,
    file_governance_to_inventory_state,
    file_governance_to_recommendation_state,
    file_governance_to_recovery_state,
    file_governance_to_version_analysis_state,
    inventory_state_to_file_governance_update,
    recommendation_state_to_file_governance_update,
    recovery_state_to_file_governance_update,
    version_analysis_state_to_file_governance_update,
)
from app.state.models import FileGovernanceState

"""本模块只定义四个业务子图、Context Compact 和 Error Recovery 的同步包装节点。"""


def run_context_compact_after_inventory(
    state: FileGovernanceState,
) -> dict:
    """在 Inventory 完成后调用 Context Compact 子图。

    该阶段只允许释放已加载且后续节点不再读取的 Prompt 正文，不压缩文档字段，
    因此不会改变 Content、Version Analysis 或 Evidence 的输入事实。

    Args:
        state: 已完成 Inventory Task 状态同步的顶层治理状态。

    Returns:
        只包含 Prompt、文档、压缩索引和错误的白名单更新。
    """
    subgraph_input = file_governance_to_context_compact_state(
        state,
        stage="after_inventory",
    )
    return execute_recoverable_subgraph(
        state,
        node_name="run_context_compact_after_inventory",
        invoke_subgraph=lambda: context_compact_graph.invoke(subgraph_input),
        convert_result=context_compact_state_to_file_governance_update,
    )


def run_context_compact_after_evidence(
    state: FileGovernanceState,
) -> dict:
    """在 Evidence 解释分派完成后调用 Context Compact 子图。

    Version Analysis 与 Evidence 已完成后，文档详细预览不再参与 Recommendation；
    子图可将其移到受控产物，同时保留 content_ref 和全部治理事实。

    Args:
        state: 已完成 Evidence Task 和固定 Evidence Subagent 分派的顶层状态。

    Returns:
        只包含 Prompt、文档、压缩索引和错误的白名单更新。
    """
    subgraph_input = file_governance_to_context_compact_state(
        state,
        stage="after_evidence",
    )
    return execute_recoverable_subgraph(
        state,
        node_name="run_context_compact_after_evidence",
        invoke_subgraph=lambda: context_compact_graph.invoke(subgraph_input),
        convert_result=context_compact_state_to_file_governance_update,
    )


def run_inventory_subgraph(state: FileGovernanceState) -> dict:
    """显式转换状态、同步执行 Inventory 子图并过滤返回字段。

    Args:
        state: 顶层文件治理状态。

    Returns:
        仅包含文件、标准化文档和错误的顶层状态更新。
    """
    subgraph_input = file_governance_to_inventory_state(state)
    return execute_recoverable_subgraph(
        state,
        node_name="run_inventory_subgraph",
        invoke_subgraph=lambda: inventory_graph.invoke(subgraph_input),
        convert_result=inventory_state_to_file_governance_update,
    )


def run_version_analysis_subgraph(state: FileGovernanceState) -> dict:
    """显式转换状态、同步执行版本分析子图并过滤返回字段。

    Version Analysis 内部通过 Team Orchestration 调用 Version Subagent；包装节点
    会把固定团队、真实 Task、Todo、Team Message 和 LLM 审计随版本事实一并
    写回，同时继续隔离比较队列、当前差异及单次分派输入输出。

    Args:
        state: 已完成文件扫描和内容提取的顶层治理状态。

    Returns:
        包含版本事实、团队协议审计、Task 进度和错误的顶层状态更新。
    """
    subgraph_input = file_governance_to_version_analysis_state(state)
    return execute_recoverable_subgraph(
        state,
        node_name="run_version_analysis_subgraph",
        invoke_subgraph=lambda: version_analysis_graph.invoke(subgraph_input),
        convert_result=version_analysis_state_to_file_governance_update,
    )


def run_evidence_subgraph(state: FileGovernanceState) -> dict:
    """显式转换状态、同步执行 Evidence 子图并过滤返回字段。

    该节点只执行确定性 PDF 来源和发送记录匹配；固定 Evidence Subagent 的解释
    分派由顶层 ``dispatch_evidence_subagent_task`` 在证据状态同步后单独执行。
    PDF 候选和原始发送日志仍由状态转换白名单隔离，不会泄漏回顶层状态。

    Args:
        state: 已具有文件、标准化文档和版本组的顶层治理状态。

    Returns:
        仅包含 PDF 来源、发送证据和错误的顶层状态更新。
    """
    subgraph_input = file_governance_to_evidence_state(state)
    return execute_recoverable_subgraph(
        state,
        node_name="run_evidence_subgraph",
        invoke_subgraph=lambda: evidence_graph.invoke(subgraph_input),
        convert_result=evidence_state_to_file_governance_update,
    )


def run_recommendation_subgraph(state: FileGovernanceState) -> dict:
    """显式转换状态、同步执行 Recommendation 子图并过滤返回字段。

    第四批已把该包装节点注册到顶层 File Governance 图，并在 Evidence 之后
    执行。候选集合等内部判断过程仍不会泄漏回顶层状态。

    Args:
        state: 已具有文件、版本关系和可选外部证据的顶层治理状态。

    Returns:
        仅包含推荐结果、人工审核状态和错误的顶层状态更新。
    """
    subgraph_input = file_governance_to_recommendation_state(state)
    return execute_recoverable_subgraph(
        state,
        node_name="run_recommendation_subgraph",
        invoke_subgraph=lambda: recommendation_graph.invoke(subgraph_input),
        convert_result=recommendation_state_to_file_governance_update,
    )


def run_error_recovery_subgraph(state: FileGovernanceState) -> dict:
    """显式转换状态、同步执行第七个 Error Recovery 子图并过滤结果。

    子图只判断复用、重试、降级、人工恢复或终止。若选择结果复用，本包装边界
    会在受控产物根目录内读取状态更新并校验结果摘要，再交给顶层条件路由续跑。

    Args:
        state: 已由直接失败边或子图异常处理入口转入恢复的顶层状态。

    Returns:
        恢复状态、错误审计、节点执行、降级、Task 及可选复用业务结果。
    """
    subgraph_input = file_governance_to_recovery_state(state)
    hydrated_input = hydrate_recovery_graph_state(
        subgraph_input,
        top_state=state,
    )
    subgraph_result = error_recovery_graph.invoke(hydrated_input)
    recovery_update = recovery_state_to_file_governance_update(subgraph_result)
    reused_update = load_recovery_reused_update(state, subgraph_result)
    if reused_update.get("errors"):
        recovery_update["errors"] = [
            *reused_update["errors"],
            *recovery_update.get("errors", []),
        ]
    if reused_update.get("tasks"):
        recovery_update["tasks"] = [
            *recovery_update.get("tasks", []),
            *reused_update["tasks"],
        ]
    return {**reused_update, **recovery_update}
