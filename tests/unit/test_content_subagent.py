from __future__ import annotations

import pytest

from app.graphs.content_subagent import content_subagent_graph
from app.llm.client import LLMClient
from app.llm.config import create_llm_config_state
from app.llm.providers.mock import MockLLMProvider
from app.state.factories import create_team_state
from app.state.models import ContentSubagentGraphState, ContentSubagentOutput

"""本模块验证 Content Subagent 的最小输入、结构化结果、回退和错误协议路径。"""

# Content 子图测试允许返回的固定标准化内容引用。
CONTENT_ARTIFACT_REF = "artifact://normalized/content-001"


def _content_state() -> ContentSubagentGraphState:
    """创建可直接调用 Content Subagent 图的完整初始状态。

    Returns:
        使用安全 Mock LLM 且 reducer 列表为空的 Content 子图状态。
    """
    return ContentSubagentGraphState(
        input={
            "task_id": "run-001:inventory",
            "document_id": "document-001",
            "content_preview": "合同编号 HT-001，金额 1000 元。",
            "structure_summary": {"paragraphs": 4, "tables": 1},
            "key_fields": {"contract_id": "HT-001", "amount": 1000},
            "artifact_refs": [CONTENT_ARTIFACT_REF],
        },
        team=create_team_state(),
        llm=create_llm_config_state(),
        system_prompt="",
        user_prompt="",
        output=None,
        fallback_used=False,
        team_messages=[],
        llm_calls=[],
        errors=[],
    )


def test_content_subagent_returns_only_summary_and_controlled_refs() -> None:
    """Content 正常路径应返回严格 Pydantic 输出及 assignment/result 消息。"""
    result = content_subagent_graph.invoke(_content_state())

    assert isinstance(result["output"], ContentSubagentOutput)
    assert set(result["output"].model_dump()) == {"summary", "artifact_refs"}
    assert set(result["output"].artifact_refs).issubset({CONTENT_ARTIFACT_REF})
    assert [message["message_type"] for message in result["team_messages"]] == [
        "assignment",
        "result",
    ]
    assert result["team_messages"][-1]["status"] == "validated"
    assert result["llm_calls"][-1]["agent_id"] == "content-subagent"
    assert "normalized_text" not in result["user_prompt"]


def test_content_subagent_uses_deterministic_fallback_on_invalid_model_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content 模型输出无效时应回退且仍返回合法 result Team Message。"""
    original_client = LLMClient

    def create_failing_client(config):
        """创建返回非法摘要的统一 LLM Client。

        Args:
            config: Content 子图传入的 LLM 配置。

        Returns:
            注入非法 Mock Provider 的 LLM Client。
        """
        return original_client(
            config,
            providers={"mock": MockLLMProvider(response_payload={"summary": ""})},
        )

    monkeypatch.setattr("app.nodes.subagents.LLMClient", create_failing_client)
    result = content_subagent_graph.invoke(_content_state())

    assert result["fallback_used"] is True
    assert "确定性内容概览" in result["output"].summary
    assert result["llm_calls"][-1]["status"] == "fallback"
    assert result["team_messages"][-1]["message_type"] == "result"


def test_content_subagent_converts_invalid_input_to_error_message() -> None:
    """Content 输入携带完整正文型字段时不得调用模型，并应返回协议错误消息。"""
    state = _content_state()
    state["input"] = dict(state["input"])
    state["input"]["full_text"] = "禁止传入的完整正文"

    result = content_subagent_graph.invoke(state)

    assert result["output"] is None
    assert result["llm_calls"] == []
    assert result["team_messages"][-1]["message_type"] == "error"
    assert result["team_messages"][-1]["error"]
