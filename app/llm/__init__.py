from app.llm.client import LLMClient, LLMInvocationResult
from app.llm.config import create_llm_config_state
from app.llm.model_profiles import (
    create_model_profile_state,
    resolve_model_profile,
)
from app.llm.prompt_loader import (
    build_dynamic_prompt_rules,
    load_system_prompt,
    mark_prompt_disabled,
    record_prompt_load_error,
)
from app.llm.schemas import (
    build_structured_output_schema,
    validate_output_artifact_refs,
    validate_structured_output,
)

"""本包集中提供 Prompt、多模型路由、统一 LLM Client 和固定 Subagent 输出校验。"""

# 本包当前允许固定 Subagent 和外部调用方直接使用的 LLM 公共接口。
__all__ = [
    "LLMClient",
    "LLMInvocationResult",
    "build_dynamic_prompt_rules",
    "build_structured_output_schema",
    "create_llm_config_state",
    "create_model_profile_state",
    "load_system_prompt",
    "mark_prompt_disabled",
    "record_prompt_load_error",
    "resolve_model_profile",
    "validate_output_artifact_refs",
    "validate_structured_output",
]
