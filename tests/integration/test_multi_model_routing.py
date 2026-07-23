from __future__ import annotations

import pytest

from app.graphs.content_subagent import content_subagent_graph
from app.graphs.evidence_subagent import evidence_subagent_graph
from app.graphs.version_subagent import version_subagent_graph
from app.llm.client import LLMClient
from app.llm.config import create_llm_config_state
from app.llm.providers.base import LLMProvider
from app.llm.providers.mock import MockLLMProvider
from app.state.factories import create_team_state
from app.state.models import (
    ContentSubagentGraphState,
    EvidenceSubagentGraphState,
    LLMConfigState,
    VersionSubagentGraphState,
)

"""本模块验证三个固定 Subagent 通过 LangGraph 节点路由不同模型 Profile。"""


class RoutedMockLLMProvider(MockLLMProvider):
    """以真实 Provider 名称审计但不访问网络的多模型路由测试替身。"""

    def __init__(self, provider_name: str) -> None:
        """创建指定审计名称的确定性 Mock Provider。

        Args:
            provider_name: 当前模型 Profile 选择的规范 Provider 名称。
        """
        super().__init__()
        self.name = provider_name
        # 模拟真实 LangChain Provider 写入审计的规范名称。


def _create_multi_model_config() -> LLMConfigState:
    """创建三个任务分别使用 Claude、DeepSeek 和 Qwen 的多 Profile 配置。

    Returns:
        由测试替身执行且不会访问网络的已启用多 Provider 配置。
    """
    return create_llm_config_state(
        {
            "enabled": True,
            "profiles": [
                {
                    "id": "content-claude",
                    "provider": "anthropic",
                    "model": "claude-content",
                },
                {
                    "id": "version-deepseek",
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "max_output_tokens": 1600,
                },
                {
                    "id": "evidence-qwen",
                    "provider": "qwen",
                    "model": "qwen-flash",
                },
            ],
            "default_profile_id": "content-claude",
            "task_profile_ids": {
                "content": "content-claude",
                "version": "version-deepseek",
                "evidence": "evidence-qwen",
            },
            "fallback_enabled": True,
        }
    )


def _create_content_state(llm: LLMConfigState) -> ContentSubagentGraphState:
    """创建 Content 多模型路由集成测试状态。

    Args:
        llm: 三个固定任务共享的多模型配置。

    Returns:
        只包含短内容预览和受控引用的 Content 子图输入状态。
    """
    return ContentSubagentGraphState(
        input={
            "task_id": "run-routing:inventory",
            "document_id": "document-001",
            "content_preview": "合同编号 HT-001，金额 1000 元。",
            "structure_summary": {"paragraphs": 4, "tables": 1},
            "key_fields": {"contract_id": "HT-001", "amount": 1000},
            "artifact_refs": ["artifact://normalized/document-001"],
        },
        team=create_team_state(),
        llm=llm,
        selected_model_profile_id="",
        system_prompt="",
        user_prompt="",
        output=None,
        fallback_used=False,
        team_messages=[],
        llm_calls=[],
        errors=[],
    )


def _create_version_state(llm: LLMConfigState) -> VersionSubagentGraphState:
    """创建 Version 多模型路由集成测试状态。

    Args:
        llm: 三个固定任务共享的多模型配置。

    Returns:
        只包含确定性差异信号和受控引用的 Version 子图输入状态。
    """
    return VersionSubagentGraphState(
        input={
            "task_id": "run-routing:version_analysis",
            "comparison_id": "comparison-001",
            "file_labels": ["合同-v1.docx", "合同-v2.docx"],
            "structural_similarity": 0.91,
            "content_similarity": 0.88,
            "key_changes": ["金额由 1000 调整为 1200"],
            "ordering_signals": ["v2 修改时间较晚"],
            "artifact_refs": ["artifact://diff/comparison-001"],
        },
        team=create_team_state(),
        llm=llm,
        selected_model_profile_id="",
        system_prompt="",
        user_prompt="",
        output=None,
        fallback_used=False,
        team_messages=[],
        llm_calls=[],
        errors=[],
    )


def _create_evidence_state(llm: LLMConfigState) -> EvidenceSubagentGraphState:
    """创建 Evidence 多模型路由集成测试状态。

    Args:
        llm: 三个固定任务共享的多模型配置。

    Returns:
        只包含证据摘要和受控引用的 Evidence 子图输入状态。
    """
    return EvidenceSubagentGraphState(
        input={
            "task_id": "run-routing:evidence",
            "group_id": "group-001",
            "pdf_evidence_summary": "PDF 与 v2 的文本和表格值高度匹配。",
            "delivery_evidence_summary": "本地发送记录按哈希匹配到 v2。",
            "artifact_refs": ["artifact://evidence/group-001"],
        },
        team=create_team_state(),
        llm=llm,
        selected_model_profile_id="",
        system_prompt="",
        user_prompt="",
        output=None,
        fallback_used=False,
        team_messages=[],
        llm_calls=[],
        errors=[],
    )


def test_three_subgraphs_route_to_distinct_model_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三个子图应跨 Provider 选择 Profile，并把真实路由信息写入调用审计。"""

    def resolve_offline_provider(
        client: LLMClient,
        profile: dict[str, object],
    ) -> LLMProvider:
        """把真实 Profile 替换成同名离线 Provider，避免集成测试产生费用。

        Args:
            client: 当前子图创建的统一 LLM Client。
            profile: 已解析的目标模型 Profile。

        Returns:
            使用目标 Provider 名称审计的确定性测试替身。
        """
        del client
        return RoutedMockLLMProvider(str(profile["provider"]))

    monkeypatch.setattr(LLMClient, "_resolve_provider", resolve_offline_provider)
    llm = _create_multi_model_config()

    content_result = content_subagent_graph.invoke(_create_content_state(llm))
    version_result = version_subagent_graph.invoke(_create_version_state(llm))
    evidence_result = evidence_subagent_graph.invoke(_create_evidence_state(llm))

    assert content_result["selected_model_profile_id"] == "content-claude"
    assert content_result["llm_calls"][-1]["model_profile_id"] == "content-claude"
    assert content_result["llm_calls"][-1]["provider"] == "anthropic"
    assert content_result["llm_calls"][-1]["model"] == "claude-content"
    assert content_result["fallback_used"] is False

    assert version_result["selected_model_profile_id"] == "version-deepseek"
    assert version_result["llm_calls"][-1]["model_profile_id"] == "version-deepseek"
    assert version_result["llm_calls"][-1]["provider"] == "deepseek"
    assert version_result["llm_calls"][-1]["model"] == "deepseek-chat"
    assert version_result["fallback_used"] is False

    assert evidence_result["selected_model_profile_id"] == "evidence-qwen"
    assert evidence_result["llm_calls"][-1]["model_profile_id"] == "evidence-qwen"
    assert evidence_result["llm_calls"][-1]["provider"] == "qwen"
    assert evidence_result["llm_calls"][-1]["model"] == "qwen-flash"
    assert evidence_result["fallback_used"] is False
