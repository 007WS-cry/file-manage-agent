from __future__ import annotations

import re
from collections.abc import Mapping
from math import isfinite
from typing import Literal, cast

from app.state.models import LLMConfigState

"""本模块校验统一 LLM 配置，并创建不包含任何 API Key 实际值的状态。"""

# 未显式配置时使用 Mock Provider，确保升级后不会自动产生外部网络调用。
DEFAULT_LLM_PROVIDER = "mock"

# Mock Provider 的默认模型标识，仅用于配置、审计和测试输出。
DEFAULT_LLM_MODEL = "mock-structured-v1"

# OpenAI Provider 默认读取的 API Key 环境变量名称。
DEFAULT_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

# 文件治理结构化摘要默认使用的低温度。
DEFAULT_LLM_TEMPERATURE = 0.0

# 单次模型结构化输出的默认最大 Token 数。
DEFAULT_LLM_MAX_OUTPUT_TOKENS = 800

# 单次模型调用的默认超时时间，单位为秒。
DEFAULT_LLM_TIMEOUT_SECONDS = 30.0

# 模型不可用时默认允许后续业务批次执行确定性回退。
DEFAULT_LLM_FALLBACK_ENABLED = True

# 允许写入配置状态的 Provider 名称集合。
SUPPORTED_LLM_PROVIDERS = frozenset({"openai", "mock"})

# API Key 环境变量名称必须符合普通跨平台环境变量命名规则。
ENVIRONMENT_VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 结构化输出 Token 上限，避免错误配置造成不受控请求。
MAX_LLM_OUTPUT_TOKENS = 32768

# 单次外部模型调用允许配置的最大超时时间，单位为秒。
MAX_LLM_TIMEOUT_SECONDS = 300.0


def _reject_unknown_fields(
    config: Mapping[str, object],
    *,
    allowed_fields: set[str],
) -> None:
    """拒绝 LLM 配置中的未知字段。

    Args:
        config: 等待校验的 LLM 配置映射。
        allowed_fields: 当前协议允许出现的字段集合。

    Raises:
        ValueError: 配置包含未知字段时抛出。
    """
    unknown_fields = sorted(set(config) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"llm_config 包含未知字段：{', '.join(unknown_fields)}")


def _normalize_positive_integer(value: object, *, field_name: str) -> int:
    """校验并返回大于零的整数配置。

    Args:
        value: 等待校验的配置值。
        field_name: 用于错误信息的完整字段名称。

    Returns:
        经过类型和取值范围校验的正整数。

    Raises:
        TypeError: 配置值不是整数或错误地使用布尔值时抛出。
        ValueError: 配置值小于一时抛出。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} 必须是整数")
    if value <= 0:
        raise ValueError(f"{field_name} 必须大于零")
    return value


def _normalize_positive_number(value: object, *, field_name: str) -> float:
    """校验并返回大于零的有限浮点配置。

    Args:
        value: 等待校验的整数或浮点配置值。
        field_name: 用于错误信息的完整字段名称。

    Returns:
        经过类型和取值范围校验的浮点数。

    Raises:
        TypeError: 配置值不是数字或错误地使用布尔值时抛出。
        ValueError: 配置值小于等于零或不是有限数字时抛出。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} 必须是数字")
    normalized = float(value)
    if normalized <= 0 or not isfinite(normalized):
        raise ValueError(f"{field_name} 必须是大于零的有限数字")
    return normalized


def create_llm_config_state(
    llm_config: Mapping[str, object] | None = None,
) -> LLMConfigState:
    """根据可选配置创建统一且不含密钥实际值的 LLM 状态。

    省略配置时完全关闭真实模型并选择 Mock Provider。OpenAI Provider 只保存环境
    变量名称，实际 API Key 由 Provider 在调用时读取，不进入 LangGraph checkpoint。

    Args:
        llm_config: 可选 LLM 配置映射；省略时使用安全关闭默认值。

    Returns:
        已完成字段、类型和安全边界校验的独立 LLM 配置状态。

    Raises:
        TypeError: 开关、字符串或数值字段类型不正确时抛出。
        ValueError: Provider、范围、环境变量名称或必需字段不合法时抛出。
    """
    config = dict(llm_config or {})
    _reject_unknown_fields(
        config,
        allowed_fields={
            "enabled",
            "provider",
            "model",
            "api_key_env",
            "temperature",
            "max_output_tokens",
            "timeout_seconds",
            "fallback_enabled",
        },
    )

    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TypeError("llm_config.enabled 必须是布尔值")

    raw_provider = config.get("provider", DEFAULT_LLM_PROVIDER)
    if not isinstance(raw_provider, str):
        raise TypeError("llm_config.provider 必须是字符串")
    provider_name = raw_provider.strip().casefold()
    if provider_name not in SUPPORTED_LLM_PROVIDERS:
        raise ValueError(
            "llm_config.provider 只能是 "
            f"{', '.join(sorted(SUPPORTED_LLM_PROVIDERS))}"
        )
    provider = cast(Literal["openai", "mock"], provider_name)

    default_model = DEFAULT_LLM_MODEL if provider == "mock" else ""
    raw_model = config.get("model", default_model)
    if not isinstance(raw_model, str):
        raise TypeError("llm_config.model 必须是字符串")
    model = raw_model.strip()
    if not model:
        raise ValueError("llm_config.model 不得为空")

    default_api_key_env = DEFAULT_OPENAI_API_KEY_ENV if provider == "openai" else None
    raw_api_key_env = config.get("api_key_env", default_api_key_env)
    if raw_api_key_env is not None and not isinstance(raw_api_key_env, str):
        raise TypeError("llm_config.api_key_env 必须是字符串或 null")
    api_key_env = raw_api_key_env.strip() if isinstance(raw_api_key_env, str) else None
    if provider == "openai" and not api_key_env:
        raise ValueError("OpenAI Provider 必须配置 api_key_env")
    if api_key_env and not ENVIRONMENT_VARIABLE_NAME_PATTERN.fullmatch(api_key_env):
        raise ValueError("llm_config.api_key_env 不是合法的环境变量名称")
    if provider == "mock" and api_key_env is not None:
        raise ValueError("Mock Provider 不允许配置 api_key_env")

    raw_temperature = config.get("temperature", DEFAULT_LLM_TEMPERATURE)
    if isinstance(raw_temperature, bool) or not isinstance(
        raw_temperature,
        (int, float),
    ):
        raise TypeError("llm_config.temperature 必须是数字")
    temperature = float(raw_temperature)
    if not isfinite(temperature) or temperature < 0 or temperature > 2:
        raise ValueError("llm_config.temperature 必须位于 0 到 2 之间")

    max_output_tokens = _normalize_positive_integer(
        config.get("max_output_tokens", DEFAULT_LLM_MAX_OUTPUT_TOKENS),
        field_name="llm_config.max_output_tokens",
    )
    if max_output_tokens > MAX_LLM_OUTPUT_TOKENS:
        raise ValueError(
            f"llm_config.max_output_tokens 不得超过 {MAX_LLM_OUTPUT_TOKENS}"
        )

    timeout_seconds = _normalize_positive_number(
        config.get("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS),
        field_name="llm_config.timeout_seconds",
    )
    if timeout_seconds > MAX_LLM_TIMEOUT_SECONDS:
        raise ValueError(
            f"llm_config.timeout_seconds 不得超过 {MAX_LLM_TIMEOUT_SECONDS}"
        )

    fallback_enabled = config.get(
        "fallback_enabled",
        DEFAULT_LLM_FALLBACK_ENABLED,
    )
    if not isinstance(fallback_enabled, bool):
        raise TypeError("llm_config.fallback_enabled 必须是布尔值")

    return LLMConfigState(
        enabled=enabled,
        provider=provider,
        model=model,
        api_key_env=api_key_env,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        fallback_enabled=fallback_enabled,
    )
