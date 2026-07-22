from __future__ import annotations

import json

from app.state.models import EvidenceSubagentInput, EvidenceSubagentOutput

"""本模块定义固定 Evidence Subagent 的职责、最小 Prompt 和确定性回退逻辑。"""

# Evidence Subagent 在 TeamState 和 LLM 审计中使用的稳定 Agent ID。
EVIDENCE_SUBAGENT_ID = "evidence-subagent"

# Evidence Subagent 负责的固定 Task 类型。
EVIDENCE_SUBAGENT_TASK_TYPES = ("evidence",)

# Evidence Subagent 的受控系统提示词，只解释已有 PDF 和发送证据摘要。
EVIDENCE_SUBAGENT_SYSTEM_PROMPT = """你是文件版本治理团队中的 Evidence Subagent。
你只能根据 PDF 来源摘要、发送记录摘要和受控产物引用解释外部证据。
不得读取完整 PDF 或业务正文，不得把相关性表述成客户确认，不得创建新的引用。
输出必须严格符合 EvidenceSubagentOutput，只包含 summary 和 artifact_refs。"""


def build_evidence_subagent_prompts(
    input_data: EvidenceSubagentInput,
) -> tuple[str, str]:
    """根据已有 PDF 和发送证据摘要生成 Evidence Subagent Prompt。

    Args:
        input_data: 已通过 Team Protocol 校验的两类证据摘要和引用。

    Returns:
        固定系统提示词和不包含原始 PDF、邮件或业务正文的 JSON 用户提示词。
    """
    prompt_payload = {
        "task_id": input_data["task_id"],
        "group_id": input_data["group_id"],
        "pdf_evidence_summary": input_data["pdf_evidence_summary"],
        "delivery_evidence_summary": input_data["delivery_evidence_summary"],
        "artifact_refs": input_data["artifact_refs"],
        "instruction": "区分 PDF 来源证据和发送证据，不夸大置信度或客户确认。",
    }
    return (
        EVIDENCE_SUBAGENT_SYSTEM_PROMPT,
        json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True),
    )


def build_deterministic_evidence_output(
    input_data: EvidenceSubagentInput,
) -> EvidenceSubagentOutput:
    """在模型不可用时合并现有证据摘要并保留证据类型边界。

    Args:
        input_data: 已通过协议校验的 Evidence 输入。

    Returns:
        只包含现有证据说明和原输入受控引用的 Pydantic 输出。
    """
    summary = (
        f"版本组 {input_data['group_id']} 的确定性证据说明："
        f"PDF 证据：{input_data['pdf_evidence_summary']}；"
        f"发送证据：{input_data['delivery_evidence_summary']}。"
    )[:4_000]
    return EvidenceSubagentOutput(
        summary=summary,
        artifact_refs=list(input_data["artifact_refs"]),
    )
