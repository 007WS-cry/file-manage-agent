from __future__ import annotations

from app.graphs.evidence_subagent import evidence_subagent_graph
from app.llm.config import create_llm_config_state
from app.state.factories import create_team_state
from app.state.models import EvidenceSubagentGraphState, EvidenceSubagentOutput

"""本模块验证 Evidence Subagent 只消费证据摘要并能用 Team Protocol 表达错误。"""

# Evidence 子图测试允许返回的固定证据产物引用。
EVIDENCE_ARTIFACT_REF = "artifact://evidence/group-001"


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
            "artifact_refs": [EVIDENCE_ARTIFACT_REF],
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
    assert set(result["output"].model_dump()) == {"summary", "artifact_refs"}
    assert set(result["output"].artifact_refs).issubset({EVIDENCE_ARTIFACT_REF})
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
    state["input"]["artifact_refs"] = ["C:/artifacts/private-evidence.json"]

    result = evidence_subagent_graph.invoke(state)

    assert "C:/artifacts/private-evidence.json" in result["user_prompt"]
    assert "normalized_text" not in result["user_prompt"]
    assert result["team_messages"][-1]["status"] == "validated"
