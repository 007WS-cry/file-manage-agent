from __future__ import annotations

import json

from app.state.models import ContentSubagentInput, ContentSubagentOutput

"""本模块定义固定 Content Subagent 的职责、最小 Prompt 和确定性回退逻辑。"""

# Content Subagent 在 TeamState 和 LLM 审计中使用的稳定 Agent ID。
CONTENT_SUBAGENT_ID = "content-subagent"

# Content Subagent 负责的固定 Task 类型。
CONTENT_SUBAGENT_TASK_TYPES = ("inventory",)

# Content Subagent 的受控系统提示词，不允许读取引用指向的完整正文。
CONTENT_SUBAGENT_SYSTEM_PROMPT = """你是文件版本治理团队中的 Content Subagent。
你只能根据输入中的短内容预览、结构摘要、关键字段和产物引用生成中文摘要。
不得请求、猜测或复述完整文档正文，不得创建新的产物引用，不得修改任何文件。
输出必须严格符合 ContentSubagentOutput，只包含 summary 和 artifact_refs。"""


def build_content_subagent_prompts(
    input_data: ContentSubagentInput,
) -> tuple[str, str]:
    """根据已校验的最小内容输入生成结构化 Prompt。

    Args:
        input_data: 已通过 Team Protocol 校验的内容预览、结构摘要和引用。

    Returns:
        固定系统提示词和不包含完整正文的 JSON 用户提示词。
    """
    prompt_payload = {
        "task_id": input_data["task_id"],
        "document_id": input_data["document_id"],
        "content_preview": input_data["content_preview"],
        "structure_summary": input_data["structure_summary"],
        "key_fields": input_data["key_fields"],
        "artifact_refs": input_data["artifact_refs"],
        "instruction": "概括文档用途、结构和关键字段；引用只能从 artifact_refs 中选择。",
    }
    return (
        CONTENT_SUBAGENT_SYSTEM_PROMPT,
        json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True),
    )


def build_deterministic_content_output(
    input_data: ContentSubagentInput,
) -> ContentSubagentOutput:
    """在模型不可用时根据结构和关键字段生成确定性内容摘要。

    Args:
        input_data: 已通过协议校验且不含完整正文的 Content 输入。

    Returns:
        只包含确定性摘要和原输入受控引用的 Pydantic 输出。
    """
    structure_names = sorted(str(key) for key in input_data["structure_summary"])
    key_field_names = sorted(str(key) for key in input_data["key_fields"])
    structure_text = "、".join(structure_names) if structure_names else "未提供结构字段"
    key_field_text = "、".join(key_field_names) if key_field_names else "未提取关键字段"
    summary = (
        f"文档 {input_data['document_id']} 的确定性内容概览："
        f"结构摘要包含 {structure_text}；关键字段包含 {key_field_text}。"
    )
    return ContentSubagentOutput(
        summary=summary,
        artifact_refs=list(input_data["artifact_refs"]),
    )
