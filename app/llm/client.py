from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel

from app.llm.config import create_llm_config_state
from app.llm.model_profiles import (
    create_disabled_mock_profile,
    resolve_model_profile,
)
from app.llm.providers.base import (
    LLMProvider,
    LLMProviderConfigurationError,
    LLMProviderTimeoutError,
)
from app.llm.providers.langchain import LangChainChatModelProvider
from app.llm.providers.mock import MockLLMProvider
from app.state.models import LLMCallRecord, LLMConfigState, ModelProfileState
from app.utils.runtime import utc_now_iso

"""本模块统一选择 LLM Provider、执行结构化调用并生成脱敏审计记录。"""

# 写入状态的模型错误摘要最大字符数，避免大型异常或响应进入 checkpoint。
MAX_LLM_AUDIT_ERROR_CHARACTERS = 300


@dataclass(frozen=True, slots=True)
class LLMInvocationResult:
    """统一 LLM Client 返回的可选结构化结果和必有审计记录。"""

    output: BaseModel | None
    # 调用成功时的 Pydantic 输出；失败或超时时为 None。

    call_record: LLMCallRecord
    # 不包含 Prompt、响应正文和 API Key 的调用审计记录。


def _normalize_required_text(value: object, *, field_name: str) -> str:
    """校验 LLM 调用使用的必需短文本参数。

    Args:
        value: 等待校验的参数值。
        field_name: 用于错误信息的字段名称。

    Returns:
        移除首尾空白后的非空字符串。

    Raises:
        TypeError: 参数不是字符串时抛出。
        ValueError: 参数为空字符串时抛出。
    """
    if not isinstance(value, str):
        raise TypeError(f"{field_name} 必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} 不得为空")
    return normalized


def _sanitize_audit_error(error: Exception) -> str:
    """生成不回显 Prompt、响应正文或 API Key 的错误摘要。

    Args:
        error: Provider 或结构化输出校验产生的异常。

    Returns:
        截断到固定长度的异常类型和安全错误消息。
    """
    error_type = type(error).__name__
    if isinstance(error, (LLMProviderConfigurationError, LLMProviderTimeoutError)):
        message = str(error).strip()
        safe_message = f"{error_type}: {message}" if message else error_type
    else:
        safe_message = f"{error_type}: LLM 调用或结构化输出校验失败"
    return safe_message[:MAX_LLM_AUDIT_ERROR_CHARACTERS]


class LLMClient:
    """按状态配置调用真实或 Mock Provider，并统一记录耗时、Token 和错误。"""

    def __init__(
        self,
        config: LLMConfigState,
        *,
        providers: Mapping[str, LLMProvider] | None = None,
    ) -> None:
        """创建一次治理运行使用的统一 LLM Client。

        Args:
            config: 已由 ``create_llm_config_state`` 校验的 LLM 配置状态。
            providers: 可选 Provider 注入映射，主要供单元测试和离线替换使用。

        Raises:
            TypeError: Provider 映射键或值类型不合法时抛出。
            ValueError: Provider 映射包含空名称时抛出。
        """
        self.config = create_llm_config_state(config)
        # 与调用方解除可变引用关系的 LLM 配置副本。

        self._providers: dict[str, LLMProvider] = {}
        # 按稳定名称保存的显式注入 Provider，不自动创建外部连接。

        for raw_name, provider in dict(providers or {}).items():
            if not isinstance(raw_name, str):
                raise TypeError("Provider 注册名称必须是字符串")
            name = raw_name.strip().casefold()
            if not name:
                raise ValueError("Provider 注册名称不得为空")
            if not isinstance(provider, LLMProvider):
                raise TypeError(f"Provider {name} 必须实现 LLMProvider")
            self._providers[name] = provider

    def _resolve_provider(self, profile: ModelProfileState) -> LLMProvider:
        """根据已解析模型 Profile 返回显式注入或 LangChain Provider。

        显式注入按 Profile ID 优先、Provider 名称次优先匹配，便于测试同一
        Provider 的多个模型路由。全部真实模型 Profile 使用 LangChain 适配器；
        API Key、Base URL 和专有参数只在调用时从声明的环境变量读取。

        Args:
            profile: 已根据任务路由和启用状态解析出的实际模型 Profile。

        Returns:
            当前调用实际使用的 Provider 实例。

        Raises:
            LLMProviderConfigurationError: Provider 名称或 Profile 配置不可用时抛出。
        """
        provider_name = profile["provider"]
        injected = self._providers.get(profile["id"]) or self._providers.get(
            provider_name
        )
        if injected is not None:
            return injected
        cache_key = f"profile:{profile['id']}"
        cached = self._providers.get(cache_key)
        if cached is not None:
            return cached
        if provider_name == "mock":
            provider = MockLLMProvider()
            self._providers[cache_key] = provider
            return provider
        try:
            provider = LangChainChatModelProvider(
                provider_name=provider_name,
                api_key_env=profile.get("api_key_env"),
                base_url_env=profile.get("base_url_env"),
                options_env=profile.get("options_env"),
                structured_output_method=profile.get(
                    "structured_output_method",
                    "auto",
                ),
            )
        except (TypeError, ValueError) as error:
            raise LLMProviderConfigurationError(
                f"模型 Profile {profile['id']} 的 Provider 配置不可用"
            ) from error
        self._providers[cache_key] = provider
        return provider

    def generate_structured(
        self,
        *,
        task_id: str,
        agent_id: str,
        message_id: str,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        model_profile_id: str | None = None,
    ) -> LLMInvocationResult:
        """按模型 Profile 执行结构化调用并始终返回可写入状态的审计记录。

        本函数不把 Prompt 或完整模型响应写入审计记录。Provider 失败、输出校验失败
        和超时会返回 ``output=None``，由后续业务节点根据 ``fallback_enabled`` 决定
        是否执行确定性回退。

        Args:
            task_id: 本次调用所属的真实 Task ID。
            agent_id: 发起调用的固定 Agent ID。
            message_id: 触发调用的 Team Message ID。
            system_prompt: 受版本控制的系统提示词。
            user_prompt: 只包含当前任务必要摘要和产物引用的提示词。
            output_model: 模型响应必须满足的 Pydantic 输出类型。
            model_profile_id: 可选显式 Profile ID；省略时使用默认 Profile。

        Returns:
            可选 Pydantic 输出和包含耗时、Token、状态及脱敏错误的审计记录。

        Raises:
            TypeError: 标识、Prompt 或输出类型不合法时抛出。
            ValueError: 标识、Prompt 或显式 Profile ID 为空或不存在时抛出。
        """
        normalized_task_id = _normalize_required_text(task_id, field_name="task_id")
        normalized_agent_id = _normalize_required_text(agent_id, field_name="agent_id")
        normalized_message_id = _normalize_required_text(
            message_id,
            field_name="message_id",
        )
        normalized_system_prompt = _normalize_required_text(
            system_prompt,
            field_name="system_prompt",
        )
        normalized_user_prompt = _normalize_required_text(
            user_prompt,
            field_name="user_prompt",
        )
        if not isinstance(output_model, type) or not issubclass(output_model, BaseModel):
            raise TypeError("output_model 必须是 Pydantic BaseModel 子类")

        configured_profile = resolve_model_profile(
            self.config,
            profile_id=model_profile_id,
        )
        effective_profile = (
            configured_profile
            if self.config["enabled"]
            else create_disabled_mock_profile()
        )
        call_id = f"llm-{uuid4().hex}"
        started_at = utc_now_iso()
        start_time = perf_counter()
        model_profile_id = effective_profile["id"]
        provider_name = effective_profile["provider"]
        model_name = effective_profile["model"]
        try:
            provider = self._resolve_provider(effective_profile)
            provider_name = provider.name
            response = provider.generate_structured(
                model=model_name,
                system_prompt=normalized_system_prompt,
                user_prompt=normalized_user_prompt,
                output_model=output_model,
                temperature=effective_profile["temperature"],
                max_output_tokens=effective_profile["max_output_tokens"],
                timeout_seconds=effective_profile["timeout_seconds"],
            )
            duration_ms = max(0, round((perf_counter() - start_time) * 1000))
            call_record = LLMCallRecord(
                id=call_id,
                task_id=normalized_task_id,
                agent_id=normalized_agent_id,
                message_id=normalized_message_id,
                model_profile_id=model_profile_id,
                provider=provider_name,
                model=model_name,
                status="success",
                started_at=started_at,
                finished_at=utc_now_iso(),
                duration_ms=duration_ms,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                total_tokens=response.total_tokens,
                error_type=None,
                error_message=None,
                fallback_used=False,
            )
            return LLMInvocationResult(
                output=response.output,
                call_record=call_record,
            )
        except Exception as error:
            duration_ms = max(0, round((perf_counter() - start_time) * 1000))
            timed_out = isinstance(error, LLMProviderTimeoutError)
            call_record = LLMCallRecord(
                id=call_id,
                task_id=normalized_task_id,
                agent_id=normalized_agent_id,
                message_id=normalized_message_id,
                model_profile_id=model_profile_id,
                provider=provider_name,
                model=model_name,
                status="timeout" if timed_out else "failed",
                started_at=started_at,
                finished_at=utc_now_iso(),
                duration_ms=duration_ms,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                error_type=type(error).__name__,
                error_message=_sanitize_audit_error(error),
                fallback_used=False,
            )
            return LLMInvocationResult(output=None, call_record=call_record)
