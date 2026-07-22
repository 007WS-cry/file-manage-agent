from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import BaseModel

"""本模块定义统一 LLM Provider 接口、返回值和不包含敏感信息的异常类型。"""


class LLMProviderError(RuntimeError):
    """表示 Provider 调用失败且未产生可用结构化输出。"""


class LLMProviderConfigurationError(LLMProviderError):
    """表示 Provider 配置、依赖或 API Key 环境变量不可用。"""


class LLMProviderTimeoutError(LLMProviderError):
    """表示 Provider 在配置的时间边界内未完成模型调用。"""


@dataclass(frozen=True, slots=True)
class LLMProviderResponse:
    """Provider 成功返回的结构化对象和 Token 使用量。"""

    output: BaseModel
    # 已经通过目标 Pydantic 模型校验的结构化输出。

    input_tokens: int | None
    # Provider 报告的输入 Token 数；无法获取时为 None。

    output_tokens: int | None
    # Provider 报告的输出 Token 数；无法获取时为 None。

    total_tokens: int | None
    # Provider 报告的总 Token 数；无法获取时为 None。


class LLMProvider(ABC):
    """所有真实和 Mock 模型 Provider 必须实现的同步结构化调用接口。"""

    name: str
    # 写入审计记录的稳定 Provider 名称。

    @abstractmethod
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
        """调用模型并返回通过 Pydantic 校验的结构化结果。

        Args:
            model: Provider 使用的模型名称。
            system_prompt: 受版本控制的系统提示词。
            user_prompt: 仅包含当前任务必要摘要和引用的用户提示词。
            output_model: 模型响应必须满足的 Pydantic 类型。
            temperature: 模型生成温度。
            max_output_tokens: 模型响应最大 Token 数。
            timeout_seconds: Provider 必须执行的调用超时边界。

        Returns:
            结构化输出及可选 Token 使用量。

        Raises:
            LLMProviderConfigurationError: Provider 依赖或配置不可用时抛出。
            LLMProviderTimeoutError: 模型调用达到超时边界时抛出。
            LLMProviderError: 其他可预期的模型或响应错误时抛出。
        """
