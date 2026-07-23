from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from functools import partial

from pydantic import BaseModel

from app.llm.providers.base import (
    LLMProvider,
    LLMProviderConfigurationError,
    LLMProviderError,
    LLMProviderResponse,
    LLMProviderStructuredOutputError,
    LLMProviderTimeoutError,
)

"""本模块通过 LangChain 统一适配主流模型、OpenAI 兼容端点和模型路由服务。"""

# 测试可注入的 LangChain Chat Model 工厂签名。
ChatModelFactory = Callable[..., object]

# LangChain ``init_chat_model`` 当前内置注册的主流 Provider。
BUILTIN_LANGCHAIN_PROVIDERS = frozenset(
    {
        "anthropic",
        "anthropic_bedrock",
        "azure_ai",
        "azure_openai",
        "baseten",
        "bedrock",
        "bedrock_converse",
        "cohere",
        "deepseek",
        "fireworks",
        "google_anthropic_vertex",
        "google_genai",
        "google_vertexai",
        "groq",
        "huggingface",
        "ibm",
        "litellm",
        "meta",
        "mistralai",
        "nvidia",
        "ollama",
        "openai",
        "openrouter",
        "perplexity",
        "together",
        "upstage",
        "xai",
    }
)

# 由本项目补充到统一工厂的 Provider：Qwen 原生集成及两类 OpenAI 兼容入口。
PROJECT_LANGCHAIN_PROVIDERS = frozenset(
    {
        "openai_compatible",
        "qwen",
        "zhipuai",
    }
)

# 可由模型 Profile 选择并交给 LangChain 创建的完整 Provider 集合。
SUPPORTED_LANGCHAIN_PROVIDERS = (
    BUILTIN_LANGCHAIN_PROVIDERS | PROJECT_LANGCHAIN_PROVIDERS
)

# 缺少可选依赖时用于生成明确安装提示的 Provider 到包名映射。
LANGCHAIN_PROVIDER_PACKAGES = {
    "anthropic": "langchain-anthropic",
    "anthropic_bedrock": "langchain-aws",
    "azure_ai": "langchain-azure-ai",
    "azure_openai": "langchain-openai",
    "baseten": "langchain-baseten",
    "bedrock": "langchain-aws",
    "bedrock_converse": "langchain-aws",
    "cohere": "langchain-cohere",
    "deepseek": "langchain-deepseek",
    "fireworks": "langchain-fireworks",
    "google_anthropic_vertex": "langchain-google-vertexai",
    "google_genai": "langchain-google-genai",
    "google_vertexai": "langchain-google-vertexai",
    "groq": "langchain-groq",
    "huggingface": "langchain-huggingface",
    "ibm": "langchain-ibm",
    "litellm": "langchain-litellm",
    "meta": "langchain-meta",
    "mistralai": "langchain-mistralai",
    "nvidia": "langchain-nvidia-ai-endpoints",
    "ollama": "langchain-ollama",
    "openai": "langchain-openai",
    "openai_compatible": "langchain-openai",
    "openrouter": "langchain-openrouter",
    "perplexity": "langchain-perplexity",
    "qwen": "langchain-qwq",
    "together": "langchain-together",
    "upstage": "langchain-upstage",
    "xai": "langchain-xai",
    "zhipuai": "langchain-openai",
}

# Provider 扩展参数环境变量允许保存的最大 JSON 字符数。
MAX_PROVIDER_OPTIONS_CHARACTERS = 16384

# 缺少实际 Base URL 时不得退回 OpenAI 官方端点的兼容 Provider。
PROVIDERS_REQUIRING_RUNTIME_BASE_URL = frozenset(
    {
        "openai_compatible",
        "zhipuai",
    }
)

# 扩展参数不得覆盖由模型 Profile 单独校验和审计的公共调用参数。
RESERVED_PROVIDER_OPTION_NAMES = frozenset(
    {
        "api_key",
        "base_url",
        "max_retries",
        "max_tokens",
        "model",
        "model_provider",
        "temperature",
        "timeout",
    }
)


def _create_langchain_chat_model(
    *,
    provider_name: str,
    **options: object,
) -> object:
    """延迟导入并创建指定 Provider 的 LangChain Chat Model。

    Qwen 使用官方文档列出的 ``ChatQwen``。GLM/ZhipuAI 当前通过维护中的
    ``ChatOpenAI`` 连接其 OpenAI 兼容端点；其余主流 Provider 交给
    ``init_chat_model`` 的内置注册表解析。

    Args:
        provider_name: 已规范化且位于支持集合中的 Provider 名称。
        options: 已完成安全校验的模型构造参数。

    Returns:
        支持 LangChain Chat Model 接口的实例。

    Raises:
        ImportError: 当前 Provider 对应的可选集成包尚未安装时抛出。
        ValueError: Provider 不在支持集合中时抛出。
    """
    if provider_name not in SUPPORTED_LANGCHAIN_PROVIDERS:
        raise ValueError(f"不支持的 LangChain Provider：{provider_name}")
    if provider_name == "qwen":
        from langchain_qwq import ChatQwen

        return ChatQwen(**options)
    if provider_name in {"openai_compatible", "zhipuai"}:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(**options)

    from langchain.chat_models import init_chat_model

    model = options.pop("model")
    if not isinstance(model, str):
        raise TypeError("LangChain 模型名称必须是字符串")
    return init_chat_model(
        model,
        model_provider=provider_name,
        **options,
    )


def _normalize_token_count(value: object) -> int | None:
    """把 Provider Token 用量规范化为非负整数。

    Args:
        value: LangChain 消息元数据中的可选 Token 数。

    Returns:
        合法非负整数；字段缺失或类型不合法时为 None。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _extract_usage(raw_message: object) -> tuple[int | None, int | None, int | None]:
    """从 LangChain AIMessage 的统一或 Provider 元数据中提取 Token 用量。

    Args:
        raw_message: ``include_raw=True`` 返回的原始 LangChain 消息。

    Returns:
        输入、输出和总 Token 数；Provider 未报告时对应位置为 None。
    """
    usage = getattr(raw_message, "usage_metadata", None)
    if isinstance(usage, Mapping):
        input_tokens = _normalize_token_count(
            usage.get("input_tokens", usage.get("prompt_tokens"))
        )
        output_tokens = _normalize_token_count(
            usage.get("output_tokens", usage.get("completion_tokens"))
        )
        total_tokens = _normalize_token_count(usage.get("total_tokens"))
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        return input_tokens, output_tokens, total_tokens

    response_metadata = getattr(raw_message, "response_metadata", None)
    if isinstance(response_metadata, Mapping):
        provider_usage = response_metadata.get("token_usage")
        if isinstance(provider_usage, Mapping):
            input_tokens = _normalize_token_count(
                provider_usage.get("prompt_tokens", provider_usage.get("input_tokens"))
            )
            output_tokens = _normalize_token_count(
                provider_usage.get(
                    "completion_tokens",
                    provider_usage.get("output_tokens"),
                )
            )
            total_tokens = _normalize_token_count(provider_usage.get("total_tokens"))
            if (
                total_tokens is None
                and input_tokens is not None
                and output_tokens is not None
            ):
                total_tokens = input_tokens + output_tokens
            return input_tokens, output_tokens, total_tokens
    return None, None, None


def _is_timeout_error(error: Exception) -> bool:
    """判断 LangChain 或底层 HTTP 异常是否表示调用超时。

    Args:
        error: LangChain、Provider SDK 或网络层抛出的异常。

    Returns:
        异常属于内置超时类型或类名包含 timeout 时为 True。
    """
    return isinstance(error, TimeoutError) or "timeout" in type(error).__name__.casefold()


def _normalize_provider_options(value: object, *, environment_name: str) -> dict[str, object]:
    """校验从环境变量读取的 Provider 专有 JSON 构造参数。

    Args:
        value: JSON 解析后的对象。
        environment_name: 仅用于安全错误信息的环境变量名称。

    Returns:
        键为非空字符串且不覆盖公共参数的独立字典。

    Raises:
        LLMProviderConfigurationError: JSON 顶层不是对象、键不合法或覆盖保留参数时抛出。
    """
    if not isinstance(value, Mapping):
        raise LLMProviderConfigurationError(
            f"Provider 扩展参数环境变量 {environment_name} 必须是 JSON 对象"
        )
    normalized: dict[str, object] = {}
    for raw_key, option_value in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise LLMProviderConfigurationError(
                f"Provider 扩展参数环境变量 {environment_name} 包含非法键"
            )
        key = raw_key.strip()
        if key in RESERVED_PROVIDER_OPTION_NAMES:
            raise LLMProviderConfigurationError(
                f"Provider 扩展参数环境变量 {environment_name} 不得覆盖 {key}"
            )
        normalized[key] = option_value
    return normalized


class LangChainChatModelProvider(LLMProvider):
    """使用 LangChain Chat Model 执行同步 Pydantic 结构化调用的 Provider。"""

    def __init__(
        self,
        *,
        provider_name: str,
        api_key_env: str | None = None,
        base_url_env: str | None = None,
        options_env: str | None = None,
        structured_output_method: str = "auto",
        model_factory: ChatModelFactory | None = None,
    ) -> None:
        """创建只保存环境变量名称、不保存凭据或端点实际值的适配器。

        Args:
            provider_name: LangChain 模型 Provider 的稳定名称。
            api_key_env: 保存 Provider API Key 的可选环境变量名称。
            base_url_env: 保存兼容服务 Base URL 的可选环境变量名称。
            options_env: 保存 Provider 专有 JSON 构造参数的可选环境变量名称。
            structured_output_method: ``auto`` 或 Provider 支持的结构化输出方法。
            model_factory: 可选 Chat Model 工厂，仅供单元测试和受控扩展注入。

        Raises:
            TypeError: 名称、环境变量或工厂类型不合法时抛出。
            ValueError: 名称为空、Provider 不支持或结构化输出方法不合法时抛出。
        """
        if not isinstance(provider_name, str):
            raise TypeError("provider_name 必须是字符串")
        normalized_provider = provider_name.strip().casefold()
        if normalized_provider not in SUPPORTED_LANGCHAIN_PROVIDERS:
            raise ValueError(
                f"不支持的 LangChain Provider：{normalized_provider or '<empty>'}"
            )
        for field_name, environment_name in (
            ("api_key_env", api_key_env),
            ("base_url_env", base_url_env),
            ("options_env", options_env),
        ):
            if environment_name is not None and not isinstance(environment_name, str):
                raise TypeError(f"{field_name} 必须是字符串或 None")
            if isinstance(environment_name, str) and not environment_name.strip():
                raise ValueError(f"{field_name} 不得为空字符串")
        if not isinstance(structured_output_method, str):
            raise TypeError("structured_output_method 必须是字符串")
        normalized_method = structured_output_method.strip().casefold()
        if normalized_method not in {
            "auto",
            "function_calling",
            "json_mode",
            "json_schema",
        }:
            raise ValueError("structured_output_method 不是受支持的方法")
        if model_factory is not None and not callable(model_factory):
            raise TypeError("model_factory 必须可调用")

        self.name = normalized_provider
        # 写入 LLMCallRecord 的稳定 Provider 名称。

        self.api_key_env = api_key_env.strip() if api_key_env else None
        # 保存 API Key 的可选环境变量名称，不保存实际密钥。

        self.base_url_env = base_url_env.strip() if base_url_env else None
        # 保存可选 Base URL 的环境变量名称，不保存地址实际值。

        self.options_env = options_env.strip() if options_env else None
        # 保存 Provider 专有参数 JSON 的环境变量名称，不保存参数实际值。

        self.structured_output_method = normalized_method
        # 选择自动、工具调用、JSON 模式或原生 JSON Schema 结构化输出。

        self._model_factory = model_factory or partial(
            _create_langchain_chat_model,
            provider_name=normalized_provider,
        )
        # 创建当前 LangChain Chat Model 的受控延迟工厂。

    def _read_api_key(self) -> str | None:
        """从声明的可选环境变量读取 API Key，且不把实际值写入异常。

        Returns:
            当前进程环境中的非空 API Key；Provider 不使用通用 Key 时为 None。

        Raises:
            LLMProviderConfigurationError: 已声明的环境变量缺失或为空时抛出。
        """
        if self.api_key_env is None:
            return None
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise LLMProviderConfigurationError(
                f"未设置模型 API Key 环境变量：{self.api_key_env}"
            )
        return api_key

    def _read_base_url(self) -> str | None:
        """从可选环境变量读取模型服务地址。

        Returns:
            去除首尾空白的 Base URL；未配置或变量为空时为 None。

        Raises:
            LLMProviderConfigurationError: 兼容 Provider 的端点环境变量未设置时抛出。
        """
        if self.base_url_env is None:
            return None
        base_url = os.environ.get(self.base_url_env)
        normalized = base_url.strip() if base_url and base_url.strip() else None
        if (
            normalized is None
            and self.name in PROVIDERS_REQUIRING_RUNTIME_BASE_URL
        ):
            raise LLMProviderConfigurationError(
                f"未设置模型 Base URL 环境变量：{self.base_url_env}"
            )
        return normalized

    def _read_provider_options(self) -> dict[str, object]:
        """从可选环境变量读取并校验 Provider 专有 JSON 参数。

        Returns:
            未配置时为空字典；否则为不覆盖公共参数的独立 JSON 对象。

        Raises:
            LLMProviderConfigurationError: 环境变量缺失、过大或不是合法 JSON 对象时抛出。
        """
        if self.options_env is None:
            return {}
        raw_options = os.environ.get(self.options_env)
        if not raw_options:
            raise LLMProviderConfigurationError(
                f"未设置 Provider 扩展参数环境变量：{self.options_env}"
            )
        if len(raw_options) > MAX_PROVIDER_OPTIONS_CHARACTERS:
            raise LLMProviderConfigurationError(
                f"Provider 扩展参数环境变量 {self.options_env} 超过长度限制"
            )
        try:
            parsed = json.loads(raw_options)
        except json.JSONDecodeError as error:
            raise LLMProviderConfigurationError(
                f"Provider 扩展参数环境变量 {self.options_env} 不是合法 JSON"
            ) from error
        return _normalize_provider_options(
            parsed,
            environment_name=self.options_env,
        )

    def _build_chat_model(
        self,
        *,
        model: str,
        temperature: float,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> object:
        """根据单次 Profile 参数创建 LangChain Chat Model。

        Args:
            model: 当前 Profile 的模型名称。
            temperature: 当前 Profile 的生成温度。
            max_output_tokens: 当前 Profile 的最大输出 Token 数。
            timeout_seconds: 当前 Profile 的调用超时秒数。

        Returns:
            尚未执行网络调用的 LangChain Chat Model。

        Raises:
            LLMProviderConfigurationError: 依赖缺失、环境配置错误或模型构造失败时抛出。
        """
        options: dict[str, object] = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_output_tokens,
            "timeout": timeout_seconds,
            "max_retries": 0,
        }
        api_key = self._read_api_key()
        if api_key is not None:
            options["api_key"] = api_key
        base_url = self._read_base_url()
        if base_url is not None:
            options["base_url"] = base_url
        options.update(self._read_provider_options())
        try:
            return self._model_factory(**options)
        except LLMProviderError:
            raise
        except ImportError as error:
            package_name = LANGCHAIN_PROVIDER_PACKAGES[self.name]
            raise LLMProviderConfigurationError(
                f"Provider {self.name} 缺少可选依赖；请安装 {package_name}"
            ) from error
        except Exception as error:
            raise LLMProviderConfigurationError(
                f"无法创建 LangChain {self.name} Chat Model"
            ) from error

    def _build_structured_model(
        self,
        *,
        chat_model: object,
        output_model: type[BaseModel],
    ) -> object:
        """按 Profile 指定的方法包装 LangChain 结构化输出。

        Args:
            chat_model: 已创建但尚未调用的 LangChain Chat Model。
            output_model: 模型响应必须满足的 Pydantic 类型。

        Returns:
            可接收消息列表并执行 ``invoke`` 的结构化 Runnable。

        Raises:
            LLMProviderStructuredOutputError: 当前模型不提供结构化输出接口时抛出。
        """
        with_structured_output = getattr(chat_model, "with_structured_output", None)
        if not callable(with_structured_output):
            raise LLMProviderStructuredOutputError(
                f"LangChain {self.name} 模型不支持结构化输出"
            )
        structured_options: dict[str, object] = {"include_raw": True}
        if self.structured_output_method != "auto":
            structured_options["method"] = self.structured_output_method
        try:
            return with_structured_output(output_model, **structured_options)
        except Exception as error:
            raise LLMProviderStructuredOutputError(
                f"LangChain {self.name} 无法启用结构化输出"
            ) from error

    def generate_structured(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        temperature: float,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> LLMProviderResponse:
        """通过 LangChain ``with_structured_output`` 返回 Pydantic 结果。

        本方法只向模型发送调用方提供的有界 Prompt，不把 Prompt、模型原始响应、
        API Key、Base URL 或 Provider 专有参数写入异常与审计。SDK 自动重试固定
        关闭，由图级确定性回退负责失败后的业务连续性。

        Args:
            model: 当前 Profile 的模型名称。
            system_prompt: 受版本控制且不含密钥的系统提示词。
            user_prompt: 只包含当前任务必要摘要和受控引用的用户提示词。
            output_model: 模型响应必须满足的 Pydantic 类型。
            temperature: 当前 Profile 的模型生成温度。
            max_output_tokens: 当前 Profile 的最大输出 Token 数。
            timeout_seconds: 当前 Profile 的调用超时秒数。

        Returns:
            已通过 Pydantic 校验的输出和可选 Token 使用量。

        Raises:
            LLMProviderConfigurationError: 依赖、凭据或 Chat Model 配置不可用时抛出。
            LLMProviderTimeoutError: LangChain 或底层 Provider 调用超时时抛出。
            LLMProviderStructuredOutputError: 返回值缺失或无法通过 Pydantic 校验时抛出。
            LLMProviderError: 其他模型调用错误时抛出。
        """
        chat_model = self._build_chat_model(
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        try:
            structured_model = self._build_structured_model(
                chat_model=chat_model,
                output_model=output_model,
            )
            response = structured_model.invoke(  # type: ignore[attr-defined]
                [
                    ("system", system_prompt),
                    ("human", user_prompt),
                ]
            )
            raw_message: object = None
            parsed: object = response
            if isinstance(response, Mapping) and (
                "parsed" in response or "parsing_error" in response
            ):
                parsing_error = response.get("parsing_error")
                if parsing_error is not None:
                    raise LLMProviderStructuredOutputError(
                        "LangChain 无法解析 Provider 结构化响应"
                    )
                parsed = response.get("parsed")
                raw_message = response.get("raw")
            if parsed is None:
                raise LLMProviderStructuredOutputError(
                    "LangChain 结构化响应缺少 parsed 结果"
                )
            try:
                output = (
                    parsed
                    if isinstance(parsed, output_model)
                    else output_model.model_validate(parsed)
                )
            except Exception as error:
                raise LLMProviderStructuredOutputError(
                    "LangChain 结构化响应未通过 Pydantic 校验"
                ) from error
            input_tokens, output_tokens, total_tokens = _extract_usage(raw_message)
            return LLMProviderResponse(
                output=output,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
        except LLMProviderError:
            raise
        except Exception as error:
            if _is_timeout_error(error):
                raise LLMProviderTimeoutError(
                    f"LangChain {self.name} 调用超过 {timeout_seconds:.3f} 秒"
                ) from error
            raise LLMProviderError(f"LangChain {self.name} 模型调用失败") from error
