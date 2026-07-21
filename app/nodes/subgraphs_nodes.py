from __future__ import annotations

from app.graphs.evidence import evidence_graph
from app.graphs.inventory import inventory_graph
from app.graphs.recommendation import recommendation_graph
from app.graphs.team_orchestration import team_orchestration_graph
from app.graphs.version_analysis import version_analysis_graph
from app.state.converters import (
    evidence_state_to_file_governance_update,
    file_governance_to_evidence_state,
    file_governance_to_inventory_state,
    file_governance_to_recommendation_state,
    file_governance_to_team_orchestration_state,
    file_governance_to_version_analysis_state,
    inventory_state_to_file_governance_update,
    recommendation_state_to_file_governance_update,
    team_orchestration_state_to_file_governance_update,
    version_analysis_state_to_file_governance_update,
)
from app.state.models import FileGovernanceState, TaskStatusUpdate

"""本模块实现五个治理子图的显式状态转换和同步调用包装节点。"""


def run_team_orchestration_subgraph(
    state: FileGovernanceState,
    *,
    task_update: TaskStatusUpdate | None = None,
) -> dict:
    """显式转换状态、同步执行 Team Orchestration 子图并过滤私有命令。

    Args:
        state: 顶层文件治理状态。
        task_update: 本次调用需要消费的可选 Task 状态更新命令。

    Returns:
        仅包含 Task、Todo 和新编排错误的顶层状态更新，不包含 task_update。
    """
    subgraph_input = file_governance_to_team_orchestration_state(
        state,
        task_update=task_update,
    )
    subgraph_result = team_orchestration_graph.invoke(subgraph_input)
    return team_orchestration_state_to_file_governance_update(subgraph_result)


def run_inventory_subgraph(state: FileGovernanceState) -> dict:
    """显式转换状态、同步执行 Inventory 子图并过滤返回字段。

    Args:
        state: 顶层文件治理状态。

    Returns:
        仅包含文件、标准化文档和错误的顶层状态更新。
    """
    subgraph_input = file_governance_to_inventory_state(state)
    subgraph_result = inventory_graph.invoke(subgraph_input)
    return inventory_state_to_file_governance_update(subgraph_result)


def run_version_analysis_subgraph(state: FileGovernanceState) -> dict:
    """显式转换状态、同步执行版本分析子图并过滤返回字段。

    Args:
        state: 已完成文件扫描和内容提取的顶层治理状态。

    Returns:
        仅包含版本组、差异、关系边、分叉、版本链和错误的顶层状态更新。
    """
    subgraph_input = file_governance_to_version_analysis_state(state)
    subgraph_result = version_analysis_graph.invoke(subgraph_input)
    return version_analysis_state_to_file_governance_update(subgraph_result)


def run_evidence_subgraph(state: FileGovernanceState) -> dict:
    """显式转换状态、同步执行 Evidence 子图并过滤返回字段。

    第四批已把该包装节点注册到顶层 File Governance 图。任务、PDF 候选和原始
    发送日志仍由状态转换白名单隔离，不会泄漏回顶层状态。

    Args:
        state: 已具有文件、标准化文档和版本组的顶层治理状态。

    Returns:
        仅包含 PDF 来源、发送证据和错误的顶层状态更新。
    """
    subgraph_input = file_governance_to_evidence_state(state)
    subgraph_result = evidence_graph.invoke(subgraph_input)
    return evidence_state_to_file_governance_update(subgraph_result)


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
    subgraph_result = recommendation_graph.invoke(subgraph_input)
    return recommendation_state_to_file_governance_update(subgraph_result)
