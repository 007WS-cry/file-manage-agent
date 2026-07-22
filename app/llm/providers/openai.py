from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel

from app.llm.providers.base import (
    LLMProvider,
    LLMProviderConfigurationError,
    LLMProviderError,
    LLMProviderResponse,
    LLMProviderTimeoutError,
)
from app.llm.schemas import validate_structured_output

"""本模块通过环境变量读取密钥并调用 OpenAI SDK 的 Pydantic 结构化输出接口。"""


class OpenAILLMProvider(LLMProvider):
    """使用 OpenAI Responses 或 Chat Completions 结构化输出的真实 Provider。"""

    name = "openai"
    # 写入 LLM 调用审计记录的固定 Provider 名称。

    def __init__(
        self,
        *,
        api_key_env: str,
        sdk_client: Any | None = None,
    ) -> None:
        """创建仅保存环境变量名称的 OpenAI Provider。

        未注入 SDK Client 时，本构造函数才从指定环境变量读取 API Key 并创建官方
        SDK Client。密钥只短暂存在于进程内存，不会写入实例字段、状态或日志。

        Args:
            api_key_env: 保存 OpenAI API Key 的环境变量名称。
            sdk_client: 测试或高级调用方注入的兼容 SDK Client。

        Raises:
            TypeError: 环境变量名称不是字符串时抛出。
            ValueError: 环境变量名称为空时抛出。
            LLMProviderConfigurationError: SDK 未安装或环境变量没有设置时抛出。
        """
        if not isinstance(api_key_env, str):
            raise TypeError("api_key_env 必须是字符串")
        normalized_env_name = api_key_env.strip()
        if not normalized_env_name:
            raise ValueError("api_key_env 不得为空")

        self.api_key_env = normalized_env_name
        # 仅保存环境变量名称，不保存 API Key 实际值。

        self._sdk_client = sdk_client or self._create_sdk_client()
        # 官方或测试注入的 SDK Client，不进入 LangGraph 状态。

    def _create_sdk_client(self) -> Any:
        """从环境变量读取密钥并创建官方 OpenAI SDK Client。

        Returns:
            已配置 API Key 的同步 OpenAI Client。

        Raises:
            LLMProviderConfigurationError: 环境变量缺失或 OpenAI SDK 未安装时抛出。
        """
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise LLMProviderConfigurationError(
                f"环境变量 {self.api_key_env} 未设置，无法调用 OpenAI Provider"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMProviderConfigurationError(
                "未安装 openai 依赖，无法创建 OpenAI Provider"
            ) from exc
        return OpenAI(api_key=api_key)

    @staticmethod
    def _read_usage_value(usage: object, *field_names: str) -> int | None:
        """从 SDK usage 对象读取第一个存在的非负 Token 数。

        Args:
            usage: Responses 或 Chat Completions 返回的 usage 对象。
            field_names: 按优先顺序尝试读取的字段名称。

        Returns:
            找到时返回非负整数 Token 数，否则返回 None。
        """
        for field_name in field_names:
            value = getattr(usage, field_name, None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    def _call_responses_api(
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
        """通过 OpenAI Responses API 调用 Pydantic 结构化输出。

        Args:
            model: OpenAI 模型名称。
            system_prompt: 受版本控制的系统提示词。
            user_prompt: 当前任务的最小必要提示词。
            output_model: 响应必须满足的 Pydantic 类型。
            temperature: 模型生成温度。
            max_output_tokens: 响应最大 Token 数。
            timeout_seconds: 单次 SDK 请求超时边界。

        Returns:
            已校验结构化输出和 SDK Token 使用量。

        Raises:
            ValueError: SDK 没有返回可解析结构化对象时抛出。
        """
        response = self._sdk_client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            text_format=output_model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout=timeout_seconds,
        )
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise ValueError("OpenAI Responses API 未返回 output_parsed")
        output = validate_structured_output(parsed, output_model)
        usage = getattr(response, "usage", None)
        return LLMProviderResponse(
            output=output,
            input_tokens=self._read_usage_value(usage, "input_tokens"),
            output_tokens=self._read_usage_value(usage, "output_tokens"),
            total_tokens=self._read_usage_value(usage, "total_tokens"),
        )

    def _call_chat_completions_api(
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
        """通过兼容的 Chat Completions parse 接口调用结构化输出。

        Args:
            model: OpenAI 模型名称。
            system_prompt: 受版本控制的系统提示词。
            user_prompt: 当前任务的最小必要提示词。
            output_model: 响应必须满足的 Pydantic 类型。
            temperature: 模型生成温度。
            max_output_tokens: 响应最大 Token 数。
            timeout_seconds: 单次 SDK 请求超时边界。

        Returns:
            已校验结构化输出和 SDK Token 使用量。

        Raises:
            ValueError: SDK 没有返回可解析结构化对象时抛出。
        """
        response = self._sdk_client.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=output_model,
            temperature=temperature,
            max_tokens=max_output_tokens,
            timeout=timeout_seconds,
        )
        choices = getattr(response, "choices", None)
        parsed = None
        if choices:
            parsed = getattr(getattr(choices[0], "message", None), "parsed", None)
        if parsed is None:
            raise ValueError("OpenAI Chat Completions API 未返回 parsed 响应")
        output = validate_structured_output(parsed, output_model)
        usage = getattr(response, "usage", None)
        return LLMProviderResponse(
            output=output,
            input_tokens=self._read_usage_value(usage, "prompt_tokens"),
            output_tokens=self._read_usage_value(usage, "completion_tokens"),
            total_tokens=self._read_usage_value(usage, "total_tokens"),
        )

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
        """调用可用的 OpenAI 结构化输出接口并统一转换错误。

        优先使用 Responses API 的 ``parse`` 接口；旧版兼容 Client 可回退到
        Chat Completions ``parse``。异常信息只保留异常类型，不回显 Prompt 或密钥。

        Args:
            model: OpenAI 模型名称。
            system_prompt: 受版本控制的系统提示词。
            user_prompt: 当前任务的最小必要提示词。
            output_model: 响应必须满足的 Pydantic 类型。
            temperature: 模型生成温度。
            max_output_tokens: 响应最大 Token 数。
            timeout_seconds: 单次 SDK 请求超时边界。

        Returns:
            结构化输出及 OpenAI SDK 报告的 Token 使用量。

        Raises:
            LLMProviderTimeoutError: SDK 报告超时时抛出。
            LLMProviderError: SDK、模型或结构化响应失败时抛出。
        """
        try:
            responses_api = getattr(self._sdk_client, "responses", None)
            if callable(getattr(responses_api, "parse", None)):
                return self._call_responses_api(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    output_model=output_model,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    timeout_seconds=timeout_seconds,
                )

            chat_api = getattr(self._sdk_client, "chat", None)
            completions_api = getattr(chat_api, "completions", None)
            if callable(getattr(completions_api, "parse", None)):
                return self._call_chat_completions_api(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    output_model=output_model,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    timeout_seconds=timeout_seconds,
                )
            raise LLMProviderConfigurationError(
                "当前 OpenAI SDK Client 不支持 Pydantic parse 结构化输出"
            )
        except LLMProviderError:
            raise
        except Exception as exc:
            error_type = type(exc).__name__
            if isinstance(exc, TimeoutError) or "timeout" in error_type.casefold():
                raise LLMProviderTimeoutError(
                    f"OpenAI Provider 调用超时（{error_type}）"
                ) from exc
            raise LLMProviderError(
                f"OpenAI Provider 调用失败（{error_type}）"
            ) from exc
