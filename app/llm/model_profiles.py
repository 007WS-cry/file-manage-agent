from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from math import isfinite

from app.llm.providers.langchain import SUPPORTED_LANGCHAIN_PROVIDERS
from app.state.models import LLMConfigState, ModelProfileState

"""本模块定义多模型 Profile 的校验、索引和按固定任务类型解析逻辑。"""

# 未显式配置时使用 Mock Provider，确保升级后不会自动产生外部网络调用。
DEFAULT_LLM_PROVIDER = "mock"

# Mock Provider 的默认模型标识，仅用于配置、审计和测试输出。
DEFAULT_LLM_MODEL = "mock-structured-v1"

# 旧版单模型配置规范化后使用的默认 Profile ID。
DEFAULT_MODEL_PROFILE_ID = "default"

# 关闭真实模型时审计记录使用的强制 Mock Profile ID。
DISABLED_MODEL_PROFILE_ID = "disabled-mock"

# OpenAI Provider 默认读取的 API Key 环境变量名称。
DEFAULT_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

# OpenAI 兼容服务地址默认读取的可选环境变量名称。
DEFAULT_OPENAI_BASE_URL_ENV = "OPENAI_BASE_URL"

# Provider 专有构造参数默认不从环境变量读取。
DEFAULT_PROVIDER_OPTIONS_ENV = None

# 默认让各 LangChain Provider 自行选择最合适的结构化输出方式。
DEFAULT_STRUCTURED_OUTPUT_METHOD = "auto"

# 文件治理结构化摘要默认使用的低温度。
DEFAULT_LLM_TEMPERATURE = 0.0

# 单次模型结构化输出的默认最大 Token 数。
DEFAULT_LLM_MAX_OUTPUT_TOKENS = 800

# 单次模型调用的默认超时时间，单位为秒。
DEFAULT_LLM_TIMEOUT_SECONDS = 30.0

# 模型不可用时默认允许后续业务批次执行确定性回退。
DEFAULT_LLM_FALLBACK_ENABLED = True

# 用户友好名称到 LangChain 规范 Provider 名称的别名映射。
LLM_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "gemini": "google_genai",
    "glm": "zhipuai",
    "openai-compatible": "openai_compatible",
}

# 0.5.2 允许写入 Profile 的全部 LangChain 主流 Provider 与离线 Mock。
SUPPORTED_LLM_PROVIDERS = SUPPORTED_LANGCHAIN_PROVIDERS | {"mock"}

# 主流 Provider 默认读取的 API Key 环境变量名称。
DEFAULT_PROVIDER_API_KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "azure_ai": "AZURE_OPENAI_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "baseten": "BASETEN_API_KEY",
    "cohere": "COHERE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
    "huggingface": "HUGGINGFACEHUB_API_TOKEN",
    "meta": "META_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "openai": DEFAULT_OPENAI_API_KEY_ENV,
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "perplexity": "PPLX_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "together": "TOGETHER_API_KEY",
    "upstage": "UPSTAGE_API_KEY",
    "xai": "XAI_API_KEY",
    "zhipuai": "ZHIPUAI_API_KEY",
}

# 具备常用可配置服务地址的 Provider 默认读取的 Base URL 环境变量名称。
DEFAULT_PROVIDER_BASE_URL_ENVS = {
    "anthropic": "ANTHROPIC_BASE_URL",
    "deepseek": "DEEPSEEK_API_BASE",
    "openai": DEFAULT_OPENAI_BASE_URL_ENV,
    "openai_compatible": "OPENAI_COMPATIBLE_BASE_URL",
    "qwen": "DASHSCOPE_API_BASE",
    "zhipuai": "ZHIPUAI_BASE_URL",
}

# 这些 Provider 没有可安全假定的唯一端点，必须显式声明 Base URL 环境变量名称。
PROVIDERS_REQUIRING_BASE_URL_ENV = frozenset(
    {
        "openai_compatible",
        "zhipuai",
    }
)

# 这些公共云 Provider 在当前适配协议中必须显式声明 API Key 环境变量名称。
PROVIDERS_REQUIRING_API_KEY_ENV = frozenset(
    {
        "anthropic",
        "deepseek",
        "google_genai",
        "openai",
        "openai_compatible",
        "openrouter",
        "qwen",
        "zhipuai",
    }
)

# 允许交给 LangChain ``with_structured_output`` 的结构化输出方法集合。
SUPPORTED_STRUCTURED_OUTPUT_METHODS = frozenset(
    {
        "auto",
        "function_calling",
        "json_mode",
        "json_schema",
    }
)

# 可以单独路由模型 Profile 的固定 Subagent 任务类型。
SUPPORTED_MODEL_TASK_TYPES = frozenset({"content", "version", "evidence"})

# Profile ID 仅允许稳定的 ASCII 标识，避免日志和 checkpoint 出现控制字符。
MODEL_PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 环境变量名称必须符合普通跨平台命名规则。
ENVIRONMENT_VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 结构化输出 Token 上限，避免错误配置造成不受控请求。
MAX_LLM_OUTPUT_TOKENS = 32768

# 单次外部模型调用允许配置的最大超时时间，单位为秒。
MAX_LLM_TIMEOUT_SECONDS = 300.0

# 单次运行允许声明的最大模型 Profile 数量。
MAX_MODEL_PROFILES = 32

# Profile 配置允许出现的字段集合。
MODEL_PROFILE_FIELDS = frozenset(
    {
        "id",
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


def _reject_unknown_fields(
    config: Mapping[str, object],
    *,
    allowed_fields: frozenset[str] | set[str],
    field_name: str,
) -> None:
    """拒绝配置映射中的未知字段。

    Args:
        config: 等待校验的配置映射。
        allowed_fields: 当前协议允许出现的字段集合。
        field_name: 用于错误信息的配置字段路径。

    Raises:
        ValueError: 配置包含未知字段时抛出。
    """
    unknown_fields = sorted(set(config) - set(allowed_fields))
    if unknown_fields:
        raise ValueError(f"{field_name} 包含未知字段：{', '.join(unknown_fields)}")


def _normalize_required_text(value: object, *, field_name: str) -> str:
    """校验并返回去除首尾空白的必需字符串。

    Args:
        value: 等待校验的配置值。
        field_name: 用于错误信息的完整字段名称。

    Returns:
        去除首尾空白后的非空字符串。

    Raises:
        TypeError: 配置值不是字符串时抛出。
        ValueError: 配置值为空字符串时抛出。
    """
    if not isinstance(value, str):
        raise TypeError(f"{field_name} 必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} 不得为空")
    return normalized


def _normalize_profile_id(value: object, *, field_name: str) -> str:
    """校验并返回可安全写入状态与日志的模型 Profile ID。

    Args:
        value: 等待校验的 Profile ID。
        field_name: 用于错误信息的完整字段名称。

    Returns:
        符合稳定 ASCII 命名规则的 Profile ID。

    Raises:
        TypeError: Profile ID 不是字符串时抛出。
        ValueError: Profile ID 为空或命名不合法时抛出。
    """
    normalized = _normalize_required_text(value, field_name=field_name)
    if not MODEL_PROFILE_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{field_name} 必须以字母或数字开头，且只能包含字母、数字、点、下划线和连字符"
        )
    return normalized


def _normalize_optional_environment_name(
    value: object,
    *,
    field_name: str,
) -> str | None:
    """校验并返回可选环境变量名称，不读取环境变量实际值。

    Args:
        value: 环境变量名称字符串或 None。
        field_name: 用于错误信息的完整字段名称。

    Returns:
        合法环境变量名称；未配置时为 None。

    Raises:
        TypeError: 配置值不是字符串或 None 时抛出。
        ValueError: 环境变量名称为空或格式不合法时抛出。
    """
    if value is None:
        return None
    normalized = _normalize_required_text(value, field_name=field_name)
    if not ENVIRONMENT_VARIABLE_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} 不是合法的环境变量名称")
    return normalized


def _normalize_temperature(value: object, *, field_name: str) -> float:
    """校验并返回位于零到二之间的有限模型温度。

    Args:
        value: 等待校验的整数或浮点数。
        field_name: 用于错误信息的完整字段名称。

    Returns:
        规范化后的浮点温度。

    Raises:
        TypeError: 配置值不是数字或错误地使用布尔值时抛出。
        ValueError: 温度不是有限数或超出零到二范围时抛出。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} 必须是数字")
    normalized = float(value)
    if not isfinite(normalized) or normalized < 0 or normalized > 2:
        raise ValueError(f"{field_name} 必须位于 0 到 2 之间")
    return normalized


def _normalize_structured_output_method(value: object, *, field_name: str) -> str:
    """校验并返回 LangChain 结构化输出方法。

    Args:
        value: 等待校验的方法名称。
        field_name: 用于错误信息的完整字段名称。

    Returns:
        小写且位于支持集合中的方法名称。

    Raises:
        TypeError: 方法名称不是字符串时抛出。
        ValueError: 方法名称不受支持时抛出。
    """
    normalized = _normalize_required_text(value, field_name=field_name).casefold()
    if normalized not in SUPPORTED_STRUCTURED_OUTPUT_METHODS:
        raise ValueError(
            f"{field_name} 只能是 "
            f"{', '.join(sorted(SUPPORTED_STRUCTURED_OUTPUT_METHODS))}"
        )
    return normalized


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


def create_model_profile_state(
    profile: Mapping[str, object],
    *,
    field_name: str = "model_profile",
) -> ModelProfileState:
    """校验单个模型 Profile，并创建不包含凭据实际值的状态。

    Args:
        profile: 包含模型、Provider、环境变量名和生成参数的 Profile 映射。
        field_name: 用于错误信息的配置字段路径。

    Returns:
        已完成字段、类型、安全边界和 Provider 约束校验的独立 Profile 状态。

    Raises:
        TypeError: Profile 字段类型不合法时抛出。
        ValueError: Profile 包含未知字段、非法范围或不支持的 Provider 时抛出。
    """
    config = dict(profile)
    _reject_unknown_fields(
        config,
        allowed_fields=MODEL_PROFILE_FIELDS,
        field_name=field_name,
    )

    profile_id = _normalize_profile_id(
        config.get("id"),
        field_name=f"{field_name}.id",
    )
    raw_provider = _normalize_required_text(
        config.get("provider", DEFAULT_LLM_PROVIDER),
        field_name=f"{field_name}.provider",
    ).casefold()
    raw_provider = LLM_PROVIDER_ALIASES.get(raw_provider, raw_provider)
    if raw_provider not in SUPPORTED_LLM_PROVIDERS:
        raise ValueError(
            f"{field_name}.provider 只能是 "
            f"{', '.join(sorted(SUPPORTED_LLM_PROVIDERS))}"
        )
    provider = raw_provider

    default_model = DEFAULT_LLM_MODEL if provider == "mock" else ""
    model = _normalize_required_text(
        config.get("model", default_model),
        field_name=f"{field_name}.model",
    )

    default_api_key_env = DEFAULT_PROVIDER_API_KEY_ENVS.get(provider)
    api_key_env = _normalize_optional_environment_name(
        config.get("api_key_env", default_api_key_env),
        field_name=f"{field_name}.api_key_env",
    )
    default_base_url_env = DEFAULT_PROVIDER_BASE_URL_ENVS.get(provider)
    base_url_env = _normalize_optional_environment_name(
        config.get("base_url_env", default_base_url_env),
        field_name=f"{field_name}.base_url_env",
    )
    options_env = _normalize_optional_environment_name(
        config.get("options_env", DEFAULT_PROVIDER_OPTIONS_ENV),
        field_name=f"{field_name}.options_env",
    )
    structured_output_method = _normalize_structured_output_method(
        config.get(
            "structured_output_method",
            DEFAULT_STRUCTURED_OUTPUT_METHOD,
        ),
        field_name=f"{field_name}.structured_output_method",
    )
    if provider in PROVIDERS_REQUIRING_API_KEY_ENV and not api_key_env:
        raise ValueError(f"{field_name}.api_key_env 是 {provider} Provider 的必需字段")
    if provider in PROVIDERS_REQUIRING_BASE_URL_ENV and not base_url_env:
        raise ValueError(f"{field_name}.base_url_env 是 {provider} Provider 的必需字段")
    if provider == "mock" and api_key_env is not None:
        raise ValueError(f"{field_name} 的 Mock Provider 不允许配置 api_key_env")
    if provider == "mock" and base_url_env is not None:
        raise ValueError(f"{field_name} 的 Mock Provider 不允许配置 base_url_env")
    if provider == "mock" and options_env is not None:
        raise ValueError(f"{field_name} 的 Mock Provider 不允许配置 options_env")
    if provider == "mock" and structured_output_method != "auto":
        raise ValueError(
            f"{field_name} 的 Mock Provider 只允许 auto 结构化输出方法"
        )

    temperature = _normalize_temperature(
        config.get("temperature", DEFAULT_LLM_TEMPERATURE),
        field_name=f"{field_name}.temperature",
    )
    max_output_tokens = _normalize_positive_integer(
        config.get("max_output_tokens", DEFAULT_LLM_MAX_OUTPUT_TOKENS),
        field_name=f"{field_name}.max_output_tokens",
    )
    if max_output_tokens > MAX_LLM_OUTPUT_TOKENS:
        raise ValueError(
            f"{field_name}.max_output_tokens 不得超过 {MAX_LLM_OUTPUT_TOKENS}"
        )
    timeout_seconds = _normalize_positive_number(
        config.get("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS),
        field_name=f"{field_name}.timeout_seconds",
    )
    if timeout_seconds > MAX_LLM_TIMEOUT_SECONDS:
        raise ValueError(
            f"{field_name}.timeout_seconds 不得超过 {MAX_LLM_TIMEOUT_SECONDS}"
        )

    return ModelProfileState(
        id=profile_id,
        provider=provider,
        model=model,
        api_key_env=api_key_env,
        base_url_env=base_url_env,
        options_env=options_env,
        structured_output_method=structured_output_method,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )


def create_legacy_model_profile(
    config: Mapping[str, object],
) -> ModelProfileState:
    """把 0.5.1 及更早的单模型配置转换成默认模型 Profile。

    Args:
        config: 已移除顶层开关和回退字段的旧版 LLM 配置映射。

    Returns:
        ID 固定为 ``default`` 的兼容模型 Profile。
    """
    profile = dict(config)
    profile["id"] = DEFAULT_MODEL_PROFILE_ID
    return create_model_profile_state(profile, field_name="llm_config")


def normalize_model_profiles(
    profiles: object,
) -> list[ModelProfileState]:
    """校验模型 Profile 列表并拒绝空列表、重复 ID 和过大配置。

    Args:
        profiles: 请求或 checkpoint 中的 Profile 列表。

    Returns:
        保持声明顺序且解除可变引用关系的 Profile 状态列表。

    Raises:
        TypeError: ``profiles`` 不是列表或元素不是映射时抛出。
        ValueError: 列表为空、过大或包含重复 Profile ID 时抛出。
    """
    if isinstance(profiles, (str, bytes)) or not isinstance(profiles, Sequence):
        raise TypeError("llm_config.profiles 必须是数组")
    if not profiles:
        raise ValueError("llm_config.profiles 不得为空")
    if len(profiles) > MAX_MODEL_PROFILES:
        raise ValueError(
            f"llm_config.profiles 数量不得超过 {MAX_MODEL_PROFILES}"
        )

    normalized_profiles: list[ModelProfileState] = []
    seen_ids: set[str] = set()
    for index, raw_profile in enumerate(profiles):
        if not isinstance(raw_profile, Mapping):
            raise TypeError(f"llm_config.profiles[{index}] 必须是对象")
        profile = create_model_profile_state(
            raw_profile,
            field_name=f"llm_config.profiles[{index}]",
        )
        if profile["id"] in seen_ids:
            raise ValueError(f"llm_config.profiles 包含重复 ID：{profile['id']}")
        seen_ids.add(profile["id"])
        normalized_profiles.append(profile)
    return normalized_profiles


def normalize_task_profile_ids(
    task_profile_ids: object,
    *,
    available_profile_ids: set[str],
) -> dict[str, str]:
    """校验固定任务类型到模型 Profile ID 的路由映射。

    Args:
        task_profile_ids: 请求或 checkpoint 中的任务路由映射。
        available_profile_ids: 当前 Profile 列表中允许引用的 ID 集合。

    Returns:
        键和值均规范化且只引用现有 Profile 的独立字典。

    Raises:
        TypeError: 路由不是映射或键值不是字符串时抛出。
        ValueError: 任务类型未知或目标 Profile 不存在时抛出。
    """
    if task_profile_ids is None:
        return {}
    if not isinstance(task_profile_ids, Mapping):
        raise TypeError("llm_config.task_profile_ids 必须是对象")

    normalized: dict[str, str] = {}
    for raw_task_type, raw_profile_id in task_profile_ids.items():
        task_type = _normalize_required_text(
            raw_task_type,
            field_name="llm_config.task_profile_ids 的键",
        ).casefold()
        if task_type not in SUPPORTED_MODEL_TASK_TYPES:
            raise ValueError(
                "llm_config.task_profile_ids 只允许任务类型 "
                f"{', '.join(sorted(SUPPORTED_MODEL_TASK_TYPES))}"
            )
        profile_id = _normalize_profile_id(
            raw_profile_id,
            field_name=f"llm_config.task_profile_ids.{task_type}",
        )
        if profile_id not in available_profile_ids:
            raise ValueError(
                f"llm_config.task_profile_ids.{task_type} 引用了不存在的 Profile："
                f"{profile_id}"
            )
        normalized[task_type] = profile_id
    return normalized


def index_model_profiles(
    profiles: Sequence[ModelProfileState],
) -> dict[str, ModelProfileState]:
    """按 Profile ID 建立深度足够的独立查询索引。

    Args:
        profiles: 已由 ``normalize_model_profiles`` 校验的 Profile 序列。

    Returns:
        Profile ID 到独立 Profile 字典的映射。
    """
    return {profile["id"]: dict(profile) for profile in profiles}


def resolve_model_profile(
    config: LLMConfigState,
    *,
    profile_id: str | None = None,
    task_type: str | None = None,
) -> ModelProfileState:
    """按显式 ID、固定任务路由或默认 ID 解析模型 Profile。

    解析优先级依次为显式 ``profile_id``、``task_profile_ids`` 中的任务映射和
    ``default_profile_id``。本函数只读取配置，不读取 API Key 或发起网络请求。

    Args:
        config: 已规范化的多模型 LLM 配置状态。
        profile_id: 调用方显式指定的可选 Profile ID。
        task_type: Content、Version 或 Evidence 固定任务类型。

    Returns:
        与配置解除可变引用关系的目标模型 Profile。

    Raises:
        TypeError: ID 或任务类型不是字符串时抛出。
        ValueError: ID、任务类型或路由引用不存在时抛出。
    """
    selected_profile_id: str
    if profile_id is not None:
        selected_profile_id = _normalize_profile_id(
            profile_id,
            field_name="model_profile_id",
        )
    elif task_type is not None:
        normalized_task_type = _normalize_required_text(
            task_type,
            field_name="task_type",
        ).casefold()
        if normalized_task_type not in SUPPORTED_MODEL_TASK_TYPES:
            raise ValueError(
                f"task_type 只能是 {', '.join(sorted(SUPPORTED_MODEL_TASK_TYPES))}"
            )
        selected_profile_id = config.get("task_profile_ids", {}).get(
            normalized_task_type,
            config["default_profile_id"],
        )
    else:
        selected_profile_id = config["default_profile_id"]

    profiles_by_id = index_model_profiles(config["profiles"])
    try:
        return profiles_by_id[selected_profile_id]
    except KeyError as error:
        raise ValueError(f"模型 Profile 不存在：{selected_profile_id}") from error


def create_disabled_mock_profile() -> ModelProfileState:
    """创建关闭真实 LLM 时强制使用的内存 Mock Profile。

    Returns:
        不含密钥或外部服务地址的固定 Mock Profile。
    """
    return create_model_profile_state(
        {
            "id": DISABLED_MODEL_PROFILE_ID,
            "provider": "mock",
            "model": DEFAULT_LLM_MODEL,
            "temperature": DEFAULT_LLM_TEMPERATURE,
            "max_output_tokens": DEFAULT_LLM_MAX_OUTPUT_TOKENS,
            "timeout_seconds": DEFAULT_LLM_TIMEOUT_SECONDS,
        },
        field_name="disabled_model_profile",
    )
