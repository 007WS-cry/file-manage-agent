from __future__ import annotations

from app.state.models import FileGovernanceState, RequestState, WorkspaceState

"""本模块负责创建可直接提交给顶层 LangGraph 的文件治理初始状态。"""


def create_initial_state(
    request: RequestState,
    workspace: WorkspaceState,
) -> FileGovernanceState:
    """创建可直接传给顶层 LangGraph 的完整初始状态。

    Args:
        request: 用户指定的扫描目录、扩展名、数量和推荐阈值。
        workspace: 只读输入根目录以及可写产物、报告目录。

    Returns:
        所有 reducer 列表和人工审核字段均已初始化的顶层状态。
    """
    return FileGovernanceState(
        run={
            "run_id": "",
            "status": "created",
            "current_stage": "created",
            "started_at": None,
            "finished_at": None,
        },
        request=dict(request),
        workspace=dict(workspace),
        human_review={
            "pending_group_ids": [],
            "selections": {},
            "review_note": None,
        },
        report={
            "summary": "",
            "report_markdown": "",
            "warnings": [],
            "report_path": None,
            "generated_at": None,
        },
        files=[],
        documents=[],
        version_groups=[],
        diffs=[],
        version_edges=[],
        branches=[],
        version_chains=[],
        decisions=[],
        errors=[],
    )
