from __future__ import annotations

from typing import Any

import pytest

from app.graphs.team_orchestration import team_orchestration_graph
from app.state.converters import file_governance_to_team_orchestration_state
from app.state.factories import create_initial_state
from app.state.models import (
    ContentSubagentInput,
    EvidenceSubagentInput,
    FileGovernanceState,
    VersionSubagentInput,
)

"""本文件集成验证 Team Orchestration 只绑定当前 Task Skill，并在分派后释放。"""

# Skills 集成测试使用的稳定运行 ID。
RUN_ID = "run-task-skill-binding"

# Skills 集成测试允许 Subagent 返回的受控产物引用。
ARTIFACT_REF = "artifact://skills/integration"


def create_top_level_state() -> FileGovernanceState:
    """创建具有 pending Skill 注册表的最小顶层治理状态。

    Returns:
        可转换为独立 Team Orchestration 输入的完整状态。
    """
    state = create_initial_state(
        {
            "root_directory": "/data/input",
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 10,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": False,
        },
        {
            "input_root": "/data/input",
            "input_readonly": True,
            "artifact_root": "/data/artifacts/content",
            "report_root": "/data/artifacts/reports",
        },
    )
    state["run"].update(
        {
            "run_id": RUN_ID,
            "status": "running",
            "current_stage": "team_orchestration",
            "started_at": "2026-07-23T00:00:00+00:00",
        }
    )
    return state


def content_request() -> ContentSubagentInput:
    """创建 Content Task 的最小分派请求。"""
    return ContentSubagentInput(
        task_id=f"{RUN_ID}:inventory",
        document_id="document-001",
        content_preview="合同编号 HT-001，金额 1000 元。",
        structure_summary={"paragraphs": 2},
        key_fields={"contract_id": "HT-001"},
        artifact_refs=[ARTIFACT_REF],
    )


def version_request() -> VersionSubagentInput:
    """创建 Version Analysis Task 的最小分派请求。"""
    return VersionSubagentInput(
        task_id=f"{RUN_ID}:version_analysis",
        comparison_id="comparison-001",
        file_labels=["contract-v1.docx", "contract-v2.docx"],
        structural_similarity=0.9,
        content_similarity=0.85,
        key_changes=["金额发生变化"],
        ordering_signals=["v2 修改时间较晚"],
        artifact_refs=[ARTIFACT_REF],
    )


def evidence_request() -> EvidenceSubagentInput:
    """创建 Evidence Task 的最小分派请求。"""
    return EvidenceSubagentInput(
        task_id=f"{RUN_ID}:evidence",
        group_id="group-001",
        pdf_evidence_summary="PDF 与 v2 高度匹配。",
        delivery_evidence_summary="发送记录按哈希匹配到 v2。",
        artifact_refs=[ARTIFACT_REF],
    )


@pytest.mark.parametrize(
    ("dispatch_request", "expected_skill_id"),
    [
        (content_request(), "file-content-analysis"),
        (version_request(), "version-relation"),
        (evidence_request(), "evidence-confidence"),
    ],
)
def test_team_graph_loads_only_current_task_skill_and_releases_after_dispatch(
    dispatch_request: ContentSubagentInput
    | VersionSubagentInput
    | EvidenceSubagentInput,
    expected_skill_id: str,
) -> None:
    """分派期间只能有一个匹配 Skill 带正文，结束后全部恢复 available。"""
    subgraph_state = file_governance_to_team_orchestration_state(
        create_top_level_state(),
        dispatch_request=dispatch_request,
    )

    snapshots = list(
        team_orchestration_graph.stream(
            subgraph_state,
            stream_mode="values",
        )
    )

    bound_snapshots = [
        snapshot
        for snapshot in snapshots
        if snapshot.get("skill_context")
    ]
    assert bound_snapshots
    bound_state: dict[str, Any] = bound_snapshots[-1]
    assert [
        instruction["skill_id"]
        for instruction in bound_state["skill_context"]
    ] == [expected_skill_id]
    bound_records = [
        skill
        for skill in bound_state["skill_registry"]["skills"]
        if skill["status"] == "bound"
    ]
    assert [skill["skill_id"] for skill in bound_records] == [expected_skill_id]
    assert bound_records[0]["content"]
    assert all(
        not skill["content"]
        for skill in bound_state["skill_registry"]["skills"]
        if skill["skill_id"] != expected_skill_id
    )

    final_state = snapshots[-1]
    assert final_state["skill_selection"] is None
    assert final_state["skill_context"] == []
    assert all(
        skill["status"] == "available"
        and skill["bound_task_id"] is None
        and skill["content"] == ""
        and skill["content_sha256"] is None
        for skill in final_state["skill_registry"]["skills"]
    )
    assert all(
        member["skill_ids"] == []
        for member in final_state["team"]["members"]
    )
