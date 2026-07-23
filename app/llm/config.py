from __future__ import annotations

from collections.abc import Mapping

from app.llm.model_profiles import (
    DEFAULT_LLM_FALLBACK_ENABLED,
    DEFAULT_LLM_MAX_OUTPUT_TOKENS,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_PROVIDER,
    DEFAULT_LLM_TEMPERATURE,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    DEFAULT_MODEL_PROFILE_ID,
    DEFAULT_OPENAI_API_KEY_ENV,
    DEFAULT_OPENAI_BASE_URL_ENV,
    DEFAULT_PROVIDER_OPTIONS_ENV,
    DEFAULT_STRUCTURED_OUTPUT_METHOD,
    ENVIRONMENT_VARIABLE_NAME_PATTERN,
    MAX_LLM_OUTPUT_TOKENS,
    MAX_LLM_TIMEOUT_SECONDS,
    SUPPORTED_LLM_PROVIDERS,
    create_legacy_model_profile,
    normalize_model_profiles,
    normalize_task_profile_ids,
)
from app.state.models import LLMConfigState

"""本模块兼容校验单模型与多模型 LLM 配置，并创建不含凭据实际值的状态。"""

# 保持 0.5.1 配置常量导入路径可用，并公开新的多模型配置工厂。
__all__ = [
    "DEFAULT_LLM_FALLBACK_ENABLED",
    "DEFAULT_LLM_MAX_OUTPUT_TOKENS",
    "DEFAULT_LLM_MODEL",
    "DEFAULT_LLM_PROVIDER",
    "DEFAULT_LLM_TEMPERATURE",
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "DEFAULT_MODEL_PROFILE_ID",
    "DEFAULT_OPENAI_API_KEY_ENV",
    "DEFAULT_OPENAI_BASE_URL_ENV",
    "DEFAULT_PROVIDER_OPTIONS_ENV",
    "DEFAULT_STRUCTURED_OUTPUT_METHOD",
    "ENVIRONMENT_VARIABLE_NAME_PATTERN",
    "MAX_LLM_OUTPUT_TOKENS",
    "MAX_LLM_TIMEOUT_SECONDS",
    "SUPPORTED_LLM_PROVIDERS",
    "create_llm_config_state",
]

# LLM 配置允许出现的顶层字段；旧单模型字段会镜像默认 Profile。
LLM_CONFIG_FIELDS = frozenset(
    {
        "enabled",
        "provider",
        "model",
        "api_key_env",
        "base_url_env",
        "options_env",
        "structured_output_method",
        "temperature",
        "max_output_tokens",
        "timeout_seconds",
        "profiles",
        "default_profile_id",
        "task_profile_ids",
        "fallback_enabled",
    }
)

# 0.5.1 及更早版本使用的单模型字段集合。
LEGACY_MODEL_FIELDS = frozenset(
    {
        "provider",
        "model",
        "api_key_env",
        "base_url_env",
        "options_env",
        "structured_output_method",
        "temperature",
        "max_output_tokens",
        "timeout_seconds",
    }
)


def _reject_unknown_fields(config: Mapping[str, object]) -> None:
    """拒绝 LLM 顶层配置中的未知字段和直接凭据值。

    Args:
        config: 等待校验的 LLM 配置映射。

    Raises:
        ValueError: 配置包含当前协议未知字段时抛出。
    """
    unknown_fields = sorted(set(config) - set(LLM_CONFIG_FIELDS))
    if unknown_fields:
        raise ValueError(f"llm_config 包含未知字段：{', '.join(unknown_fields)}")


def _normalize_enabled(value: object) -> bool:
    """校验并返回真实模型启用开关。

    Args:
        value: 等待校验的启用开关。

    Returns:
        类型合法的布尔开关。

    Raises:
        TypeError: 配置值不是布尔值时抛出。
    """
    if not isinstance(value, bool):
        raise TypeError("llm_config.enabled 必须是布尔值")
    return value


def _normalize_fallback_enabled(value: object) -> bool:
    """校验并返回确定性回退开关。

    Args:
        value: 等待校验的回退开关。

    Returns:
        类型合法的布尔开关。

    Raises:
        TypeError: 配置值不是布尔值时抛出。
    """
    if not isinstance(value, bool):
        raise TypeError("llm_config.fallback_enabled 必须是布尔值")
    return value


def _normalize_default_profile_id(
    value: object,
    *,
    available_profile_ids: set[str],
    first_profile_id: str,
) -> str:
    """校验默认 Profile ID 并确认目标存在。

    Args:
        value: 显式默认 ID；省略时传入 None。
        available_profile_ids: 已校验 Profile ID 集合。
        first_profile_id: 未显式配置时使用的首个 Profile ID。

    Returns:
        存在于当前 Profile 列表中的默认 ID。

    Raises:
        TypeError: 默认 ID 不是字符串时抛出。
        ValueError: 默认 ID 为空或引用不存在时抛出。
    """
    if value is None:
        return (
            DEFAULT_MODEL_PROFILE_ID
            if DEFAULT_MODEL_PROFILE_ID in available_profile_ids
            else first_profile_id
        )
    if not isinstance(value, str):
        raise TypeError("llm_config.default_profile_id 必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise ValueError("llm_config.default_profile_id 不得为空")
    if normalized not in available_profile_ids:
        raise ValueError(
            f"llm_config.default_profile_id 引用了不存在的 Profile：{normalized}"
        )
    return normalized


def _validate_compatibility_mirrors(
    config: Mapping[str, object],
    *,
    default_profile: Mapping[str, object],
) -> None:
    """确认多 Profile 配置中的旧字段与默认 Profile 完全一致。

    已规范化状态需要同时携带旧兼容镜像和 ``profiles``，因此不能简单拒绝两种字段
    共存；但原始请求若声明冲突值必须失败，避免调用方误以为冲突字段会生效。

    Args:
        config: 可能同时包含新旧字段的 LLM 配置。
        default_profile: ``default_profile_id`` 指向的已校验 Profile。

    Raises:
        ValueError: 任一显式旧字段与默认 Profile 值不一致时抛出。
    """
    conflicting_fields = sorted(
        field_name
        for field_name in LEGACY_MODEL_FIELDS
        if field_name in config and config[field_name] != default_profile[field_name]
    )
    if conflicting_fields:
        raise ValueError(
            "多 Profile 配置中的旧兼容字段必须与默认 Profile 一致："
            f"{', '.join(conflicting_fields)}"
        )


def create_llm_config_state(
    llm_config: Mapping[str, object] | None = None,
) -> LLMConfigState:
    """根据单模型或多模型配置创建统一且不含凭据实际值的 LLM 状态。

    旧版 ``provider/model`` 配置会自动转换成 ID 为 ``default`` 的单一 Profile；
    新版可声明多个 ``profiles``，再用 ``task_profile_ids`` 为三个固定 Subagent
    分别路由。API Key 和 Base URL 均只保存环境变量名称，实际值不进入 checkpoint。

    Args:
        llm_config: 可选 LLM 配置映射；省略时使用关闭真实模型的安全 Mock 默认值。

    Returns:
        已完成兼容转换、路由引用和安全边界校验的独立 LLM 配置状态。

    Raises:
        TypeError: 开关、Profile、路由或字段类型不正确时抛出。
        ValueError: 未知字段、Provider、范围、环境变量名称或引用不合法时抛出。
    """
    config = dict(llm_config or {})
    _reject_unknown_fields(config)

    enabled = _normalize_enabled(config.get("enabled", False))
    fallback_enabled = _normalize_fallback_enabled(
        config.get("fallback_enabled", DEFAULT_LLM_FALLBACK_ENABLED)
    )

    if "profiles" in config:
        profiles = normalize_model_profiles(config["profiles"])
    else:
        legacy_profile_config = {
            field_name: config[field_name]
            for field_name in LEGACY_MODEL_FIELDS
            if field_name in config
        }
        profiles = [create_legacy_model_profile(legacy_profile_config)]

    available_profile_ids = {profile["id"] for profile in profiles}
    default_profile_id = _normalize_default_profile_id(
        config.get("default_profile_id"),
        available_profile_ids=available_profile_ids,
        first_profile_id=profiles[0]["id"],
    )
    task_profile_ids = normalize_task_profile_ids(
        config.get("task_profile_ids"),
        available_profile_ids=available_profile_ids,
    )
    profiles_by_id = {profile["id"]: profile for profile in profiles}
    default_profile = profiles_by_id[default_profile_id]
    if "profiles" in config:
        _validate_compatibility_mirrors(
            config,
            default_profile=default_profile,
        )

    return LLMConfigState(
        enabled=enabled,
        provider=default_profile["provider"],
        model=default_profile["model"],
        api_key_env=default_profile["api_key_env"],
        base_url_env=default_profile["base_url_env"],
        options_env=default_profile["options_env"],
        structured_output_method=default_profile["structured_output_method"],
        temperature=default_profile["temperature"],
        max_output_tokens=default_profile["max_output_tokens"],
        timeout_seconds=default_profile["timeout_seconds"],
        profiles=[dict(profile) for profile in profiles],
        default_profile_id=default_profile_id,
        task_profile_ids=dict(task_profile_ids),
        fallback_enabled=fallback_enabled,
    )
