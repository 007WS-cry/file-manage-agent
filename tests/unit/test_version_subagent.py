from __future__ import annotations

import pytest

from app.graphs.version_subagent import version_subagent_graph
from app.llm.client import LLMClient
from app.llm.config import create_llm_config_state
from app.llm.providers.mock import MockLLMProvider
from app.state.factories import create_team_state
from app.state.models import VersionSubagentGraphState, VersionSubagentOutput

"""本模块验证 Version Subagent 的差异输入、引用白名单和确定性回退路径。"""

# Version 子图测试使用的固定文件对产物引用。
VERSION_ARTIFACT_REFS = [
    "artifact://normalized/version-001",
    "artifact://normalized/version-002",
]


def _version_state() -> VersionSubagentGraphState:
    """创建可直接调用 Version Subagent 图的完整初始状态。

    Returns:
        只包含文件标签、相似度、差异信号和引用的 Version 子图状态。
    """
    return VersionSubagentGraphState(
        input={
            "task_id": "run-001:version_analysis",
            "comparison_id": "comparison-001",
            "file_labels": ["合同-v1.docx", "合同-v2.docx"],
            "structural_similarity": 0.91,
            "content_similarity": 0.88,
            "key_changes": ["金额由 1000 调整为 1200"],
            "ordering_signals": ["v2 修改时间较晚"],
            "artifact_refs": list(VERSION_ARTIFACT_REFS),
        },
        team=create_team_state(),
        llm=create_llm_config_state(),
        selected_model_profile_id="",
        system_prompt="",
        user_prompt="",
        output=None,
        fallback_used=False,
        team_messages=[],
        llm_calls=[],
        errors=[],
    )


def test_version_subagent_returns_strict_structured_output() -> None:
    """Version 正常路径应返回摘要、受控引用和合法 result 消息。"""
    result = version_subagent_graph.invoke(_version_state())

    assert isinstance(result["output"], VersionSubagentOutput)
    assert set(result["output"].model_dump()) == {"summary", "artifact_refs"}
    assert set(result["output"].artifact_refs).issubset(set(VERSION_ARTIFACT_REFS))
    assert result["team_messages"][-1]["message_type"] == "result"
    assert result["llm_calls"][-1]["agent_id"] == "version-subagent"


def test_version_subagent_falls_back_when_model_invents_artifact_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Version 模型伪造产物引用时应拒绝输出并使用确定性差异摘要。"""
    original_client = LLMClient

    def create_inventing_client(config):
        """创建返回白名单外引用的统一 LLM Client。

        Args:
            config: Version 子图传入的 LLM 配置。

        Returns:
            注入引用伪造 Mock Provider 的 LLM Client。
        """
        return original_client(
            config,
            providers={
                "mock": MockLLMProvider(
                    response_payload={
                        "summary": "模型版本摘要",
                        "artifact_refs": ["artifact://invented/version"],
                    }
                )
            },
        )

    monkeypatch.setattr("app.nodes.subagents.LLMClient", create_inventing_client)
    result = version_subagent_graph.invoke(_version_state())

    assert result["fallback_used"] is True
    assert "确定性比较摘要" in result["output"].summary
    assert result["output"].artifact_refs == VERSION_ARTIFACT_REFS
    assert result["team_messages"][-1]["message_type"] == "result"


def test_version_subagent_rejects_non_pair_file_labels() -> None:
    """Version 输入不是恰好两个文件标签时应返回 error Team Message。"""
    state = _version_state()
    state["input"] = dict(state["input"])
    state["input"]["file_labels"] = ["only-one.docx"]

    result = version_subagent_graph.invoke(state)

    assert result["output"] is None
    assert result["team_messages"][-1]["message_type"] == "error"
    assert result["llm_calls"] == []
