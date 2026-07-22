from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.protocol import create_result_message
from app.graphs.team_orchestration import team_orchestration_graph
from app.llm.client import LLMClient
from app.llm.config import create_llm_config_state
from app.llm.providers.mock import MockLLMProvider
from app.state.factories import create_team_state
from app.state.models import ContentSubagentOutput, TeamOrchestrationGraphState

"""本模块验证 Subagent 失败或返回越权引用时由协调者安全执行确定性回退。"""

# 协调者回退测试使用的固定运行 ID。
RUN_ID = "run-subagent-coordinator-fallback-001"

# 协调者回退允许保留的输入产物引用。
ALLOWED_ARTIFACT_REF = "artifact://normalized/content-fallback-001"

# 模拟恶意或错误 Subagent 返回的白名单外引用。
FORGED_ARTIFACT_REF = "artifact://forged/not-allowed"


def _fallback_state() -> TeamOrchestrationGraphState:
    """创建可以触发 Content 分派与协调者回退的完整编排状态。

    Returns:
        关闭角色子图内部回退、但保留 Team Orchestration 回退能力的状态。
    """
    return TeamOrchestrationGraphState(
        run={
            "run_id": RUN_ID,
            "status": "running",
            "current_stage": "team_orchestration",
            "started_at": "2026-07-22T09:00:00+00:00",
            "finished_at": None,
        },
        llm=create_llm_config_state({"fallback_enabled": False}),
        team=create_team_state(),
        task_update=None,
        dispatch_request={
            "task_id": f"{RUN_ID}:inventory",
            "document_id": "document-fallback-001",
            "content_preview": "只用于协议验证的短预览。",
            "structure_summary": {"paragraphs": 2},
            "key_fields": {"document_type": "合同"},
            "artifact_refs": [ALLOWED_ARTIFACT_REF],
        },
        dispatch_result=None,
        tasks=[],
        todos=[],
        team_messages=[],
        llm_calls=[],
        errors=[],
    )


def test_coordinator_falls_back_when_subagent_model_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """角色子图返回 error 消息时应生成确定性 result 并更新 fallback 审计。"""
    original_client = LLMClient

    def create_failing_client(config):
        """创建始终返回非法 Pydantic 摘要的 Mock LLM Client。

        Args:
            config: Content 子图接收的关闭内部回退 LLM 配置。

        Returns:
            结构化输出必定失败但不访问网络的统一 LLM Client。
        """
        return original_client(
            config,
            providers={"mock": MockLLMProvider(response_payload={"summary": ""})},
        )

    monkeypatch.setattr("app.nodes.subagents.LLMClient", create_failing_client)
    result = team_orchestration_graph.invoke(_fallback_state())

    assert isinstance(result["dispatch_result"], ContentSubagentOutput)
    assert "确定性内容概览" in result["dispatch_result"].summary
    assert [message["message_type"] for message in result["team_messages"]] == [
        "assignment",
        "error",
        "result",
    ]
    assert result["team_messages"][-1]["artifact_refs"] == [ALLOWED_ARTIFACT_REF]
    assert result["llm_calls"][-1]["status"] == "fallback"
    assert result["llm_calls"][-1]["fallback_used"] is True
    assert all(member["status"] == "idle" for member in result["team"]["members"])
    assert not any(error["fatal"] for error in result["errors"])


def test_coordinator_rejects_forged_ref_and_rebuilds_controlled_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subagent 返回白名单外引用时不得合并该引用，并应改用确定性结果。"""

    def invoke_forged_subgraph(state):
        """模拟返回格式合法但产物引用越权的 Content 子图。

        Args:
            state: Team Orchestration 构造的隔离 Content 子图状态。

        Returns:
            包含伪造引用的 Pydantic 输出和 result Team Message。
        """
        output = ContentSubagentOutput(
            summary="伪造引用的模型摘要。",
            artifact_refs=[FORGED_ARTIFACT_REF],
        )
        message = create_result_message(
            team=state["team"],
            task_id=state["input"]["task_id"],
            sender="content-subagent",
            summary=output.summary,
            artifact_refs=output.artifact_refs,
        )
        return {
            "output": output,
            "team_messages": [*state["team_messages"], message],
            "llm_calls": [],
            "errors": [],
        }

    fake_graph = SimpleNamespace(invoke=invoke_forged_subgraph)
    monkeypatch.setattr(
        "app.nodes.team_orchestration.content_subagent_graph",
        fake_graph,
    )
    result = team_orchestration_graph.invoke(_fallback_state())

    assert FORGED_ARTIFACT_REF not in result["dispatch_result"].artifact_refs
    assert result["dispatch_result"].artifact_refs == [ALLOWED_ARTIFACT_REF]
    assert result["team_messages"][-1]["message_type"] == "result"
    assert result["team_messages"][-1]["artifact_refs"] == [ALLOWED_ARTIFACT_REF]
    task = next(item for item in result["tasks"] if item["task_type"] == "inventory")
    assert FORGED_ARTIFACT_REF not in task["output_refs"]
    assert ALLOWED_ARTIFACT_REF in task["output_refs"]
    assert result["llm_calls"][-1]["status"] == "fallback"
    assert any(
        error["node_name"] == "validate_team_message"
        and error["category"] == "protocol"
        for error in result["errors"]
    )


def test_subgraph_exception_is_sanitized_before_coordinator_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子图异常携带疑似正文时不得把原始消息写入编排错误或 Team Message。"""

    def invoke_crashing_subgraph(state):
        """模拟异常文本中意外包含业务正文的 Content 子图。

        Args:
            state: Team Orchestration 构造的隔离 Content 子图状态。

        Raises:
            RuntimeError: 始终抛出用于验证脱敏边界的异常。
        """
        del state
        raise RuntimeError("完整正文：这段敏感业务内容不得进入 checkpoint")

    fake_graph = SimpleNamespace(invoke=invoke_crashing_subgraph)
    monkeypatch.setattr(
        "app.nodes.team_orchestration.content_subagent_graph",
        fake_graph,
    )
    result = team_orchestration_graph.invoke(_fallback_state())

    serialized_errors = " ".join(error["message"] for error in result["errors"])
    serialized_messages = " ".join(
        message["summary"] + (message["error"] or "")
        for message in result["team_messages"]
    )
    assert "敏感业务内容" not in serialized_errors
    assert "敏感业务内容" not in serialized_messages
    assert result["dispatch_result"].artifact_refs == [ALLOWED_ARTIFACT_REF]
    assert result["llm_calls"][-1]["status"] == "fallback"
