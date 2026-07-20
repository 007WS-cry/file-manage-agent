from app.llm.prompt_loader import (
    build_dynamic_prompt_rules,
    load_system_prompt,
    mark_prompt_disabled,
    record_prompt_load_error,
)

"""本包集中提供模型调用前使用的 Prompt 加载与后续 LLM 扩展入口。"""

# 本包当前允许外部直接调用的 System Prompt 公共接口。
__all__ = [
    "build_dynamic_prompt_rules",
    "load_system_prompt",
    "mark_prompt_disabled",
    "record_prompt_load_error",
]
