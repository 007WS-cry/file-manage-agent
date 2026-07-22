from __future__ import annotations

from collections.abc import Mapping

import pytest
from pydantic import BaseModel

from app.graphs.team_orchestration import team_orchestration_graph
from app.llm.client import LLMClient
from app.llm.config import create_llm_config_state
from app.llm.providers.mock import MockLLMProvider
from app.state.factories import create_team_state
from app.state.models import (
    ContentSubagentOutput,
    EvidenceSubagentOutput,
    TeamOrchestrationGraphState,
    VersionSubagentOutput,
)

"""本模块集成验证 Team Orchestration 对三个固定 Subagent 的选择、调用和结果合并。"""

# 三角色分派测试共享的运行 ID。
RUN_ID = "run-agent-team-dispatch-001"

# 三角色分派测试共享的带时区运行开始时间。
STARTED_AT = "2026-07-22T08:00:00+00:00"

# Mock 输出和 Task 共同登记的受控产物引用。
CONTROLLED_ARTIFACT_REF = "artifact://subagent/controlled-result-001"


def _dispatch_state(dispatch_request: Mapping[str, object]) -> TeamOrchestrationGraphState:
    """创建包含单个角色分派请求的完整 Team Orchestration 状态。

    Args:
        dispatch_request: Content、Version 或 Evidence 的最小输入信封。

    Returns:
        使用固定 Team、安全 Mock 配置和空 reducer 列表的独立编排状态。
    """
    return TeamOrchestrationGraphState(
        run={
            "run_id": RUN_ID,
            "status": "running",
            "current_stage": "team_orchestration",
            "started_at": STARTED_AT,
            "finished_at": None,
        },
        llm=create_llm_config_state(),
        team=create_team_state(),
        task_update=None,
        dispatch_request=dict(dispatch_request),
        dispatch_result=None,
        tasks=[],
        todos=[],
        team_messages=[],
        llm_calls=[],
        errors=[],
    )


def _content_request() -> dict[str, object]:
    """创建只包含短预览和受控引用的 Content 分派请求。"""
    return {
        "task_id": f"{RUN_ID}:inventory",
        "document_id": "document-001",
        "content_preview": "合同编号 HT-001，金额 1000 元。",
        "structure_summary": {"paragraphs": 4, "tables": 1},
        "key_fields": {"contract_id": "HT-001", "amount": 1000},
        "artifact_refs": [CONTROLLED_ARTIFACT_REF],
    }


def _version_request() -> dict[str, object]:
    """创建只包含确定性比较结果的 Version 分派请求。"""
    return {
        "task_id": f"{RUN_ID}:version_analysis",
        "comparison_id": "comparison-001",
        "file_labels": ["合同-v1.docx", "合同-v2.docx"],
        "structural_similarity": 0.92,
        "content_similarity": 0.87,
        "key_changes": ["金额由 1000 调整为 1200"],
        "ordering_signals": ["文件名版本号由 v1 变为 v2"],
        "artifact_refs": [CONTROLLED_ARTIFACT_REF],
    }


def _evidence_request() -> dict[str, object]:
    """创建只包含 PDF 与发送摘要的 Evidence 分派请求。"""
    return {
        "task_id": f"{RUN_ID}:evidence",
        "group_id": "group-001",
        "pdf_evidence_summary": "PDF 与合同 v2 的内容指纹相符。",
        "delivery_evidence_summary": "发送日志记录了合同 v2 的本地路径。",
        "artifact_refs": [CONTROLLED_ARTIFACT_REF],
    }


@pytest.mark.parametrize(
    ("dispatch_request", "expected_agent_id", "expected_task_type", "output_model"),
    [
        (_content_request(), "content-subagent", "inventory", ContentSubagentOutput),
        (
            _version_request(),
            "version-subagent",
            "version_analysis",
            VersionSubagentOutput,
        ),
        (_evidence_request(), "evidence-subagent", "evidence", EvidenceSubagentOutput),
    ],
)
def test_orchestration_dispatches_to_exactly_one_fixed_subagent(
    monkeypatch: pytest.MonkeyPatch,
    dispatch_request: Mapping[str, object],
    expected_agent_id: str,
    expected_task_type: str,
    output_model: type[BaseModel],
) -> None:
    """三类 Task 应选择唯一固定角色并合并摘要、引用和审计。"""
    original_client = LLMClient

    def create_controlled_client(config):
        """创建返回当前输入白名单引用的 Mock LLM Client。

        Args:
            config: Team Orchestration 传给角色子图的 LLM 配置。

        Returns:
            使用固定摘要、引用和 Token 计数的统一 LLM Client。
        """
        return original_client(
            config,
            providers={
                "mock": MockLLMProvider(
                    response_payload={
                        "summary": "Mock 固定 Subagent 已生成受控摘要。",
                        "artifact_refs": [CONTROLLED_ARTIFACT_REF],
                    },
                    input_tokens=20,
                    output_tokens=10,
                )
            },
        )

    monkeypatch.setattr("app.nodes.subagents.LLMClient", create_controlled_client)
    result = team_orchestration_graph.invoke(_dispatch_state(dispatch_request))

    assert isinstance(result["dispatch_result"], output_model)
    assert result["dispatch_request"] is None
    assert [message["message_type"] for message in result["team_messages"]] == [
        "assignment",
        "result",
    ]
    assert len({message["message_id"] for message in result["team_messages"]}) == 2
    assert result["team_messages"][-1]["sender"] == expected_agent_id
    task = next(
        item for item in result["tasks"] if item["task_type"] == expected_task_type
    )
    assert CONTROLLED_ARTIFACT_REF in task["output_refs"]
    assert result["llm_calls"][-1]["agent_id"] == expected_agent_id
    assert result["llm_calls"][-1]["status"] == "success"
    assert all(member["status"] == "idle" for member in result["team"]["members"])
    assert not any(error["fatal"] for error in result["errors"])


def test_orchestration_rejects_dynamic_team_members() -> None:
    """TeamState 出现动态成员时必须在调用任何 Subagent 前停止。"""
    state = _dispatch_state(_content_request())
    state["team"] = dict(state["team"])
    state["team"]["members"] = [
        *state["team"]["members"],
        {
            "id": "dynamic-agent",
            "role": "content",
            "status": "idle",
            "current_task_id": None,
            "tool_names": [],
            "skill_ids": [],
        },
    ]

    result = team_orchestration_graph.invoke(state)

    assert result["team_messages"] == []
    assert result["llm_calls"] == []
    assert any(
        error["node_name"] == "initialize_fixed_agent_team" and error["fatal"]
        for error in result["errors"]
    )


def test_orchestration_rejects_worktree_tool_configuration() -> None:
    """0.4.4 固定成员不得提前配置 Worktree 或其他工具能力。"""
    state = _dispatch_state(_content_request())
    state["team"] = dict(state["team"])
    state["team"]["members"] = [dict(member) for member in state["team"]["members"]]
    state["team"]["members"][1]["tool_names"] = ["prepare_task_worktree"]

    result = team_orchestration_graph.invoke(state)

    assert result["team_messages"] == []
    assert any(
        error["node_name"] == "initialize_fixed_agent_team"
        and "Worktree" in error["message"]
        for error in result["errors"]
    )
