from __future__ import annotations

import json

from app.state.models import VersionSubagentInput, VersionSubagentOutput

"""本模块定义固定 Version Subagent 的职责、最小 Prompt 和确定性回退逻辑。"""

# Version Subagent 在 TeamState 和 LLM 审计中使用的稳定 Agent ID。
VERSION_SUBAGENT_ID = "version-subagent"

# Version Subagent 负责的固定 Task 类型。
VERSION_SUBAGENT_TASK_TYPES = ("version_analysis",)

# Version Subagent 的受控系统提示词，只解释确定性比较结果。
VERSION_SUBAGENT_SYSTEM_PROMPT = """你是文件版本治理团队中的 Version Subagent。
你只能根据文件安全标签、相似度、关键修改、排序信号和产物引用解释版本差异。
不得读取或请求完整文档正文，不得凭空推断修改，不得创建新的产物引用。
输出必须严格符合 VersionSubagentOutput，只包含 summary 和 artifact_refs。"""


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
        "structural_similarity": input_data["structural_similarity"],
        "content_similarity": input_data["content_similarity"],
        "key_changes": input_data["key_changes"],
        "ordering_signals": input_data["ordering_signals"],
        "artifact_refs": input_data["artifact_refs"],
        "instruction": "解释关键修改和先后信号；不新增比较事实或产物引用。",
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
        artifact_refs=list(input_data["artifact_refs"]),
    )
