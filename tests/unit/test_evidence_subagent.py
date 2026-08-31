from __future__ import annotations

import pytest

from app.graphs.evidence_subagent import evidence_subagent_graph
from app.llm.client import LLMClient
from app.llm.config import create_llm_config_state
from app.llm.providers.mock import MockLLMProvider
from app.state.factories import create_team_state
from app.state.models import EvidenceSubagentGraphState, EvidenceSubagentOutput

"""本模块验证 Evidence Subagent 只消费证据摘要并能用 Team Protocol 表达错误。"""

# Evidence 子图测试允许返回的固定证据产物引用。
EVIDENCE_ARTIFACT_REF = "artifact://evidence/group-001"

# Evidence 业务语义输出允许引用的固定受控摘录引用。
BUSINESS_EVIDENCE_REF = "local-log://delivery-001:sentence-1"


def _evidence_state() -> EvidenceSubagentGraphState:
    """创建可直接调用 Evidence Subagent 图的完整初始状态。

    Returns:
        只包含 PDF、发送证据摘要和引用的 Evidence 子图状态。
    """
    return EvidenceSubagentGraphState(
        input={
            "task_id": "run-001:evidence",
            "group_id": "group-001",
            "pdf_evidence_summary": "PDF 与 v2 的文本和表格值高度匹配。",
            "delivery_evidence_summary": "本地发送记录按哈希匹配到 v2。",
            "evidence_snippets": [
                {
                    "evidence_ref": BUSINESS_EVIDENCE_REF,
                    "target_file_id": "contract-v2",
                    "source": "local_log",
                    "text": "报价已确认，请以附件中的第二版为准。",
                    "effective_time": "2026-07-12T10:30:00+08:00",
                }
            ],
            "artifact_refs": [EVIDENCE_ARTIFACT_REF, BUSINESS_EVIDENCE_REF],
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


def test_evidence_subagent_returns_only_summary_and_controlled_refs() -> None:
    """Evidence 正常路径应返回严格输出且 Prompt 不含原始 PDF 正文。"""
    result = evidence_subagent_graph.invoke(_evidence_state())

    assert isinstance(result["output"], EvidenceSubagentOutput)
    assert set(result["output"].model_dump()) == {
        "summary",
        "business_evidence",
        "artifact_refs",
    }
    assert set(result["output"].artifact_refs).issubset(
        {EVIDENCE_ARTIFACT_REF, BUSINESS_EVIDENCE_REF}
    )
    assert result["team_messages"][-1]["message_type"] == "result"
    assert result["llm_calls"][-1]["agent_id"] == "evidence-subagent"
    assert "raw_pdf_text" not in result["user_prompt"]


def test_evidence_subagent_converts_raw_pdf_input_to_protocol_error() -> None:
    """Evidence 输入试图携带原始 PDF 文本时应直接返回 error 消息。"""
    state = _evidence_state()
    state["input"] = dict(state["input"])
    state["input"]["raw_pdf_text"] = "禁止传入的完整 PDF 文本"

    result = evidence_subagent_graph.invoke(state)

    assert result["output"] is None
    assert result["llm_calls"] == []
    assert result["team_messages"][-1]["message_type"] == "error"
    assert "协议外字段" in result["team_messages"][-1]["error"]


def test_evidence_subagent_prompt_uses_summaries_not_artifact_contents() -> None:
    """Evidence Prompt 可以包含引用名称，但不得读取或嵌入引用文件内容。"""
    state = _evidence_state()
    state["input"] = dict(state["input"])
    state["input"]["artifact_refs"] = [
        "C:/artifacts/private-evidence.json",
        BUSINESS_EVIDENCE_REF,
    ]

    result = evidence_subagent_graph.invoke(state)

    assert "C:/artifacts/private-evidence.json" in result["user_prompt"]
    assert "normalized_text" not in result["user_prompt"]
    assert result["team_messages"][-1]["status"] == "validated"


def test_evidence_subagent_accepts_grounded_business_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合法业务证据分类必须复用受控摘录中的文件、时间和引用。"""
    original_client = LLMClient

    def create_business_evidence_client(config):
        """创建返回合法明确批准语义候选的 Mock Client。"""
        return original_client(
            config,
            providers={
                "mock": MockLLMProvider(
                    response_payload={
                        "summary": "客户明确确认第二版报价。",
                        "business_evidence": [
                            {
                                "evidence_type": "approved",
                                "target_file_id": "contract-v2",
                                "status": "approved",
                                "actor_role": "customer",
                                "effective_time": "2026-07-12T10:30:00+08:00",
                                "reason": "摘录明确表达报价已确认。",
                                "evidence_refs": [BUSINESS_EVIDENCE_REF],
                                "confidence": 0.96,
                            }
                        ],
                        "artifact_refs": [BUSINESS_EVIDENCE_REF],
                    }
                )
            },
        )

    monkeypatch.setattr(
        "app.nodes.subagents.LLMClient",
        create_business_evidence_client,
    )
    result = evidence_subagent_graph.invoke(_evidence_state())

    analysis = result["output"].business_evidence[0]
    assert result["fallback_used"] is False
    assert analysis.evidence_type == "approved"
    assert analysis.target_file_id == "contract-v2"
    assert analysis.evidence_refs == [BUSINESS_EVIDENCE_REF]


def test_evidence_subagent_rejects_invented_business_evidence_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型伪造业务证据引用时必须拒绝候选并进入确定性回退。"""
    original_client = LLMClient

    def create_inventing_client(config):
        """创建返回白名单外业务证据引用的 Mock Client。"""
        return original_client(
            config,
            providers={
                "mock": MockLLMProvider(
                    response_payload={
                        "summary": "客户已确认。",
                        "business_evidence": [
                            {
                                "evidence_type": "approved",
                                "target_file_id": "contract-v2",
                                "status": "approved",
                                "actor_role": "customer",
                                "effective_time": None,
                                "reason": "引用了不存在的证据。",
                                "evidence_refs": ["email:invented:sentence-1"],
                                "confidence": 0.96,
                            }
                        ],
                        "artifact_refs": [],
                    }
                )
            },
        )

    monkeypatch.setattr("app.nodes.subagents.LLMClient", create_inventing_client)
    result = evidence_subagent_graph.invoke(_evidence_state())

    assert result["fallback_used"] is True
    assert result["output"].business_evidence == []
