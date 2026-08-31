from __future__ import annotations

import json

from app.state.models import EvidenceSubagentInput, EvidenceSubagentOutput

"""本模块定义固定 Evidence Subagent 的职责、最小 Prompt 和确定性回退逻辑。"""

# Evidence Subagent 在 TeamState 和 LLM 审计中使用的稳定 Agent ID。
EVIDENCE_SUBAGENT_ID = "evidence-subagent"

# Evidence Subagent 负责的固定 Task 类型。
EVIDENCE_SUBAGENT_TASK_TYPES = ("evidence",)

# Evidence Subagent 的受控系统提示词，只解释摘要和用户提供的有界证据摘录。
EVIDENCE_SUBAGENT_SYSTEM_PROMPT = """你是文件版本治理团队中的 Evidence Subagent。
你只能根据 PDF 来源摘要、发送记录摘要、有界 evidence_snippets 和受控引用解释外部证据。
不得读取或推断完整 PDF、完整邮件或业务文件正文，不得创建新的文件 ID、时间或证据引用。
business_evidence 只能使用 approved、rejected、superseded、for_reference_only、
requires_revision、final_version、sent_but_unconfirmed、ambiguous 八种 evidence_type。
status 必须依次对应 approved、rejected、superseded、reference_only、revision_required、
final、unconfirmed、ambiguous。target_file_id、effective_time 和 evidence_refs 必须逐项来自
所引用 evidence_snippets；无法可靠判断时使用 ambiguous，不得输出评分、排除、审核或其他系统动作。
输出必须严格符合 EvidenceSubagentOutput。"""


def build_evidence_subagent_prompts(
    input_data: EvidenceSubagentInput,
) -> tuple[str, str]:
    """根据证据摘要和有界业务摘录生成 Evidence Subagent Prompt。

    Args:
        input_data: 已通过 Team Protocol 校验的摘要、受控摘录和引用。

    Returns:
        固定系统提示词和不包含完整 PDF、邮件或业务正文的 JSON 用户提示词。
    """
    prompt_payload = {
        "task_id": input_data["task_id"],
        "group_id": input_data["group_id"],
        "pdf_evidence_summary": input_data["pdf_evidence_summary"],
        "delivery_evidence_summary": input_data["delivery_evidence_summary"],
        "evidence_snippets": input_data["evidence_snippets"],
        "artifact_refs": input_data["artifact_refs"],
        "instruction": (
            "逐条识别受控摘录的业务证据类型；只提出候选分类，"
            "不得输出规则动作或夸大置信度。"
        ),
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
        只包含现有证据说明、空业务候选和原输入受控引用的 Pydantic 输出。
    """
    summary = (
        f"版本组 {input_data['group_id']} 的确定性证据说明："
        f"PDF 证据：{input_data['pdf_evidence_summary']}；"
        f"发送证据：{input_data['delivery_evidence_summary']}。"
    )[:4_000]
    return EvidenceSubagentOutput(
        summary=summary,
        business_evidence=[],
        artifact_refs=list(input_data["artifact_refs"]),
    )
