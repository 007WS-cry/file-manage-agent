from __future__ import annotations

from app.graphs.inventory import inventory_graph
from app.graphs.version_analysis import version_analysis_graph
from app.state.converters import (
    file_governance_to_inventory_state,
    file_governance_to_version_analysis_state,
    inventory_state_to_file_governance_update,
    version_analysis_state_to_file_governance_update,
)
from app.state.models import FileGovernanceState

"""本模块仅实现调用 Inventory 与 Version Analysis 子图的顶层包装节点。"""


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
        仅包含版本业务结果、人工审核和错误的顶层状态更新。
    """
    subgraph_input = file_governance_to_version_analysis_state(state)
    subgraph_result = version_analysis_graph.invoke(subgraph_input)
    return version_analysis_state_to_file_governance_update(subgraph_result)
