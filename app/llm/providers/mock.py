from __future__ import annotations

import time
from copy import deepcopy

from pydantic import BaseModel

from app.llm.providers.base import (
    LLMProvider,
    LLMProviderResponse,
    LLMProviderTimeoutError,
)
from app.llm.schemas import validate_structured_output

"""本模块提供不访问网络的确定性 Mock LLM Provider，用于测试和安全关闭模式。"""

# 默认 Mock 结构化结果适配三个固定 Subagent 的最小摘要协议。
DEFAULT_MOCK_RESPONSE_PAYLOAD = {
    "summary": "Mock LLM 已生成结构化摘要。",
    "artifact_refs": [],
}


def _validate_token_count(value: int | None, *, field_name: str) -> int | None:
    """校验 Mock Provider 配置的可选 Token 数。

    Args:
        value: 等待校验的非负整数或 None。
        field_name: 用于错误信息的字段名称。

    Returns:
        通过校验的原始 Token 数。

    Raises:
        TypeError: Token 数不是整数或 None 时抛出。
        ValueError: Token 数小于零时抛出。
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} 必须是整数或 None")
    if value < 0:
        raise ValueError(f"{field_name} 不得小于零")
    return value


class MockLLMProvider(LLMProvider):
    """返回预设 Pydantic 结果并可确定性模拟耗时与超时的 Provider。"""

    name = "mock"
    # 写入 LLM 调用审计记录的固定 Provider 名称。

    def __init__(
        self,
        *,
        response_payload: object | None = None,
        latency_seconds: float = 0.0,
        input_tokens: int | None = 12,
        output_tokens: int | None = 8,
    ) -> None:
        """创建一项不访问网络的 Mock Provider 配置。

        Args:
            response_payload: 返回给目标 Pydantic 模型校验的预设对象。
            latency_seconds: 调用前模拟的耗时，单位为秒。
            input_tokens: 写入审计记录的模拟输入 Token 数。
            output_tokens: 写入审计记录的模拟输出 Token 数。

        Raises:
            TypeError: 模拟耗时或 Token 配置类型不合法时抛出。
            ValueError: 模拟耗时或 Token 数小于零时抛出。
        """
        if isinstance(latency_seconds, bool) or not isinstance(
            latency_seconds,
            (int, float),
        ):
            raise TypeError("latency_seconds 必须是数字")
        if latency_seconds < 0:
            raise ValueError("latency_seconds 不得小于零")

        self.response_payload = deepcopy(
            DEFAULT_MOCK_RESPONSE_PAYLOAD
            if response_payload is None
            else response_payload
        )
        # 每次调用都深复制的预设结构化返回对象。

        self.latency_seconds = float(latency_seconds)
        # 模拟 Provider 调用耗时，单位为秒。

        self.input_tokens = _validate_token_count(
            input_tokens,
            field_name="input_tokens",
        )
        # 模拟输入 Token 数。

        self.output_tokens = _validate_token_count(
            output_tokens,
            field_name="output_tokens",
        )
        # 模拟输出 Token 数。

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
        """返回预设结构化对象，并按照配置模拟正常耗时或超时。

        Args:
            model: 审计使用的 Mock 模型名称。
            system_prompt: 测试调用使用的系统提示词。
            user_prompt: 测试调用使用的最小用户提示词。
            output_model: Mock 返回值必须满足的 Pydantic 类型。
            temperature: 与真实 Provider 对齐但不影响确定性返回。
            max_output_tokens: 与真实 Provider 对齐的输出 Token 上限。
            timeout_seconds: 允许的最大模拟耗时。

        Returns:
            通过目标 Pydantic 模型校验的结果和模拟 Token 使用量。

        Raises:
            LLMProviderTimeoutError: 模拟耗时达到或超过超时边界时抛出。
            ValueError: 预设对象无法通过目标 Pydantic 模型校验时抛出。
        """
        del model, system_prompt, user_prompt, temperature, max_output_tokens
        if self.latency_seconds >= timeout_seconds:
            raise LLMProviderTimeoutError(
                f"Mock Provider 在 {timeout_seconds:.3f} 秒边界内未完成调用"
            )
        if self.latency_seconds:
            time.sleep(self.latency_seconds)

        output = validate_structured_output(
            deepcopy(self.response_payload),
            output_model,
        )
        total_tokens = (
            self.input_tokens + self.output_tokens
            if self.input_tokens is not None and self.output_tokens is not None
            else None
        )
        return LLMProviderResponse(
            output=output,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=total_tokens,
        )
