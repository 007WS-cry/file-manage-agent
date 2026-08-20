from __future__ import annotations

import json

from app.state.models import VersionSubagentInput, VersionSubagentOutput

"""本模块定义固定 Version Subagent 的职责、最小 Prompt 和确定性回退逻辑。"""

# Version Subagent 在 TeamState 和 LLM 审计中使用的稳定 Agent ID。
VERSION_SUBAGENT_ID = "version-subagent"

# Version Subagent 负责的固定 Task 类型。
VERSION_SUBAGENT_TASK_TYPES = ("version_analysis",)

# Version Subagent 的受控系统提示词，只能提出受证据约束的语义和关系候选。
VERSION_SUBAGENT_SYSTEM_PROMPT = """你是文件版本治理团队中的 Version Subagent。
你只能根据文件安全标签、相似度、关键修改、有界差异证据、排序信号、确定性关系、关系约束和关系证据解释版本差异。
你必须把有业务意义的差异分类到以下封闭 change_type：amount、date_or_term、responsible_party、delivery_scope、payment_term、breach_liability、approval_status、contact_or_recipient、wording_only、formatting_only、no_material_change。
每项 semantic_changes 必须包含 significance（low/medium/high）、old_value、new_value、business_impact、evidence_refs 和 0 到 1 的 confidence；evidence_refs 只能引用输入 change_evidence 中已有的 diff: 引用。
你还可以输出一个 relation_assessment，把文件对判断为以下封闭关系之一：direct_revision（直接修订）、parallel_branch（并行分支）、derived_export（导出版本）、semantic_duplicate（语义重复）、unrelated（无关文件）、uncertain（无法判断）。
relation_assessment 必须包含 relation、reason、evidence_refs 和 0 到 1 的 confidence；除 uncertain 外至少引用一项输入 relation_evidence 中已有的 diff: 引用。同组 context 只能作为辅助证据，不得凭空建立父子关系。
relation_constraints 是不可覆盖的硬约束：exact_hash_match 为 true 时必须判断 semantic_duplicate；parent_child_supported 为 false 时不得提出 direct_revision；export_supported 为 false 时不得提出 derived_export；semantic_duplicate_supported、parallel_branch_supported 或 unrelated_supported 为 false 时不得提出对应关系。证据不足时输出 uncertain。
version_order_known 为 false 时不得把候选 A/B 猜测为新旧版本，old_value 和 new_value 应为 null 或在摘要中明确方向未知。
不得读取或请求完整文档正文，不得凭空推断修改，不得创建新的证据或产物引用，不得输出审核动作、审核优先级或版本图修改动作。
输出必须严格符合 VersionSubagentOutput，只包含 summary、semantic_changes、relation_assessment 和 artifact_refs。"""


def build_version_subagent_prompts(
    input_data: VersionSubagentInput,
) -> tuple[str, str]:
    """根据确定性文件对比较结果生成 Version Subagent Prompt。

    Args:
        input_data: 已通过 Team Protocol 校验的文件标签、差异和排序信号。

    Returns:
        固定系统提示词和不包含候选文件正文的 JSON 用户提示词。
    """
    prompt_payload = {
        "task_id": input_data["task_id"],
        "comparison_id": input_data["comparison_id"],
        "file_labels": input_data["file_labels"],
        "version_order_known": input_data["version_order_known"],
        "structural_similarity": input_data["structural_similarity"],
        "content_similarity": input_data["content_similarity"],
        "key_changes": input_data["key_changes"],
        "change_evidence": input_data["change_evidence"],
        "deterministic_relation": input_data["deterministic_relation"],
        "deterministic_relation_confidence": input_data[
            "deterministic_relation_confidence"
        ],
        "relation_constraints": input_data["relation_constraints"],
        "relation_evidence": input_data["relation_evidence"],
        "ordering_signals": input_data["ordering_signals"],
        "artifact_refs": input_data["artifact_refs"],
        "instruction": (
            "解释关键修改和先后信号，并逐项输出可复核的业务语义分类；"
            "在硬约束内提出一个可复核的关系候选；同一证据不要重复分类，"
            "不新增比较事实、证据引用、产物引用或系统动作。"
        ),
    }
    return (
        VERSION_SUBAGENT_SYSTEM_PROMPT,
        json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True),
    )


def build_deterministic_version_output(
    input_data: VersionSubagentInput,
) -> VersionSubagentOutput:
    """在模型不可用时复用确定性差异和排序信号生成版本摘要。

    Args:
        input_data: 已通过协议校验的 Version 输入。

    Returns:
        不增加推断事实、只携带原输入引用的 Pydantic 输出。
    """
    changes = "；".join(input_data["key_changes"]) or "未发现明确关键字段变化"
    ordering = "；".join(input_data["ordering_signals"]) or "缺少可靠先后信号"
    labels = " 与 ".join(input_data["file_labels"])
    summary = (
        f"{labels} 的确定性比较摘要：关键修改为 {changes}；"
        f"版本先后证据为 {ordering}。"
    )[:4_000]
    return VersionSubagentOutput(
        summary=summary,
        semantic_changes=[],
        relation_assessment=None,
        artifact_refs=list(input_data["artifact_refs"]),
    )
