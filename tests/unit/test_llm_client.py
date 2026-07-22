from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.entrypoints.cli import resolve_llm_payload
from app.llm.client import LLMClient
from app.llm.config import create_llm_config_state
from app.llm.providers.base import LLMProviderConfigurationError
from app.llm.providers.mock import MockLLMProvider
from app.llm.providers.openai import OpenAILLMProvider
from app.state.models import ContentSubagentOutput

"""本模块验证统一 LLM Client、真实 Provider 适配、Mock、超时和脱敏审计。"""

# 单元测试使用的固定 Task ID。
TEST_TASK_ID = "run-001:inventory"

# 单元测试使用的固定 Agent ID。
TEST_AGENT_ID = "content-subagent"

# 单元测试使用的固定 Team Message ID。
TEST_MESSAGE_ID = "message-001"


class FakeResponsesAPI:
    """模拟 OpenAI Responses parse 接口并记录实际请求参数。"""

    def __init__(self) -> None:
        """创建尚未收到调用的 Fake Responses API。"""
        self.last_request: dict | None = None
        # 最近一次 parse 请求参数；调用前为 None。

    def parse(self, **kwargs: object) -> SimpleNamespace:
        """返回包含 Pydantic 输出和 Token 用量的模拟 SDK 响应。

        Args:
            kwargs: OpenAI Provider 传入的结构化调用参数。

        Returns:
            具有 ``output_parsed`` 和 ``usage`` 字段的模拟 SDK 响应。
        """
        self.last_request = dict(kwargs)
        return SimpleNamespace(
            output_parsed=ContentSubagentOutput(
                summary="真实 Provider 适配器测试摘要。",
                artifact_refs=["artifact://document/1"],
            ),
            usage=SimpleNamespace(
                input_tokens=21,
                output_tokens=9,
                total_tokens=30,
            ),
        )


class FakeOpenAIClient:
    """只公开 Responses API 的最小 OpenAI SDK 兼容 Client。"""

    def __init__(self) -> None:
        """创建测试专用 Responses API。"""
        self.responses = FakeResponsesAPI()
        # OpenAI Provider 优先调用的 Responses API 对象。


class FakeChatCompletionsAPI:
    """模拟公开的 Chat Completions parse 兼容接口。"""

    def __init__(self) -> None:
        """创建尚未收到调用的 Fake Chat Completions API。"""
        self.last_request: dict | None = None
        # 最近一次 parse 请求参数；调用前为 None。

    def parse(self, **kwargs: object) -> SimpleNamespace:
        """返回包含 Pydantic 输出和 Token 用量的兼容响应。

        Args:
            kwargs: OpenAI Provider 传入的 Chat Completions 调用参数。

        Returns:
            具有 ``choices`` 和 ``usage`` 字段的模拟 SDK 响应。
        """
        self.last_request = dict(kwargs)
        parsed = ContentSubagentOutput(
            summary="Chat Completions 兼容路径测试摘要。",
            artifact_refs=["artifact://document/1"],
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
            usage=SimpleNamespace(
                prompt_tokens=18,
                completion_tokens=7,
                total_tokens=25,
            ),
        )


class FakeChatOpenAIClient:
    """只公开 Chat Completions API 的最小兼容 Client。"""

    def __init__(self) -> None:
        """创建测试专用 Chat Completions API 层级。"""
        self.chat = SimpleNamespace(completions=FakeChatCompletionsAPI())
        # OpenAI Provider 在 Responses API 不可用时调用的公开兼容接口。


def _invoke(client: LLMClient):
    """使用固定标识和最小 Prompt 调用统一 LLM Client。

    Args:
        client: 等待测试的统一 LLM Client。

    Returns:
        ``ContentSubagentOutput`` 对应的统一调用结果。
    """
    return client.generate_structured(
        task_id=TEST_TASK_ID,
        agent_id=TEST_AGENT_ID,
        message_id=TEST_MESSAGE_ID,
        system_prompt="你是只读文件内容分析助手。",
        user_prompt="仅根据摘要解释关键字段。",
        output_model=ContentSubagentOutput,
    )


def test_default_config_uses_mock_without_external_call() -> None:
    """默认关闭真实模型时应使用 Mock Provider 并返回成功审计。"""
    config = create_llm_config_state()
    result = _invoke(LLMClient(config))

    assert isinstance(result.output, ContentSubagentOutput)
    assert result.output.summary == "Mock LLM 已生成结构化摘要。"
    assert result.call_record["provider"] == "mock"
    assert result.call_record["status"] == "success"
    assert result.call_record["input_tokens"] == 12
    assert result.call_record["output_tokens"] == 8
    assert result.call_record["total_tokens"] == 20
    assert result.call_record["duration_ms"] >= 0
    assert result.call_record["error_message"] is None
    assert "只读文件内容分析助手" not in repr(result.call_record)
    assert "仅根据摘要解释关键字段" not in repr(result.call_record)


def test_disabled_openai_configuration_is_forced_to_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Provider 处于关闭状态时不得读取密钥或产生外部调用。"""
    monkeypatch.delenv("DISABLED_OPENAI_API_KEY", raising=False)
    config = create_llm_config_state(
        {
            "enabled": False,
            "provider": "openai",
            "model": "configured-but-disabled-model",
            "api_key_env": "DISABLED_OPENAI_API_KEY",
        }
    )

    result = _invoke(LLMClient(config))

    assert isinstance(result.output, ContentSubagentOutput)
    assert result.call_record["provider"] == "mock"
    assert result.call_record["status"] == "success"


def test_mock_timeout_returns_timeout_audit_without_sleeping() -> None:
    """Mock 模拟耗时超过边界时应返回 timeout 且没有结构化结果。"""
    config = create_llm_config_state(
        {
            "enabled": True,
            "provider": "mock",
            "model": "mock-timeout",
            "timeout_seconds": 0.01,
        }
    )
    provider = MockLLMProvider(latency_seconds=0.02)

    result = _invoke(LLMClient(config, providers={"mock": provider}))

    assert result.output is None
    assert result.call_record["status"] == "timeout"
    assert result.call_record["error_type"] == "LLMProviderTimeoutError"
    assert "0.010" in str(result.call_record["error_message"])


def test_invalid_mock_payload_returns_failed_audit() -> None:
    """Mock 返回非法 Pydantic 字段时应记录失败而不是抛出到业务图。"""
    config = create_llm_config_state(
        {"enabled": True, "provider": "mock", "model": "mock-invalid"}
    )
    provider = MockLLMProvider(response_payload={"summary": ""})

    result = _invoke(LLMClient(config, providers={"mock": provider}))

    assert result.output is None
    assert result.call_record["status"] == "failed"
    assert result.call_record["error_type"] == "ValueError"
    assert "结构化输出校验失败" in str(result.call_record["error_message"])


def test_openai_provider_uses_structured_api_and_records_usage() -> None:
    """OpenAI 适配器应传递模型参数并读取结构化结果和 Token 用量。"""
    config = create_llm_config_state(
        {
            "enabled": True,
            "provider": "openai",
            "model": "configured-model",
            "api_key_env": "TEST_OPENAI_API_KEY",
            "temperature": 0.1,
            "max_output_tokens": 256,
            "timeout_seconds": 12.5,
        }
    )
    sdk_client = FakeOpenAIClient()
    provider = OpenAILLMProvider(
        api_key_env="TEST_OPENAI_API_KEY",
        sdk_client=sdk_client,
    )

    result = _invoke(LLMClient(config, providers={"openai": provider}))

    assert isinstance(result.output, ContentSubagentOutput)
    assert result.call_record["provider"] == "openai"
    assert result.call_record["model"] == "configured-model"
    assert result.call_record["total_tokens"] == 30
    assert sdk_client.responses.last_request is not None
    assert sdk_client.responses.last_request["text_format"] is ContentSubagentOutput
    assert sdk_client.responses.last_request["timeout"] == 12.5


def test_openai_provider_uses_public_chat_parse_as_compatibility_path() -> None:
    """Responses API 不可用时应回退到公开 Chat Completions parse 接口。"""
    config = create_llm_config_state(
        {
            "enabled": True,
            "provider": "openai",
            "model": "compatible-model",
            "api_key_env": "TEST_OPENAI_API_KEY",
            "max_output_tokens": 128,
        }
    )
    sdk_client = FakeChatOpenAIClient()
    provider = OpenAILLMProvider(
        api_key_env="TEST_OPENAI_API_KEY",
        sdk_client=sdk_client,
    )

    result = _invoke(LLMClient(config, providers={"openai": provider}))

    assert isinstance(result.output, ContentSubagentOutput)
    assert result.call_record["total_tokens"] == 25
    assert sdk_client.chat.completions.last_request is not None
    assert (
        sdk_client.chat.completions.last_request["response_format"]
        is ContentSubagentOutput
    )
    assert sdk_client.chat.completions.last_request["max_tokens"] == 128


def test_openai_provider_requires_api_key_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未注入 Client 时 OpenAI Provider 必须只从指定环境变量读取密钥。"""
    monkeypatch.delenv("MISSING_OPENAI_API_KEY", raising=False)

    with pytest.raises(LLMProviderConfigurationError, match="MISSING_OPENAI_API_KEY"):
        OpenAILLMProvider(api_key_env="MISSING_OPENAI_API_KEY")


def test_llm_config_never_accepts_api_key_actual_value() -> None:
    """LLM 配置协议必须拒绝直接传入 API Key 实际值的字段。"""
    secret_value = "secret-value-must-not-enter-state"

    with pytest.raises(ValueError, match="api_key"):
        create_llm_config_state(
            {
                "enabled": True,
                "provider": "openai",
                "model": "configured-model",
                "api_key_env": "OPENAI_API_KEY",
                "api_key": secret_value,
            }
        )

    assert secret_value not in repr(create_llm_config_state())


def test_cli_resolves_llm_object_without_reading_environment() -> None:
    """CLI 应只复制 llm 配置对象，不读取密钥或改变调用方字典。"""
    raw_config = {
        "enabled": True,
        "provider": "openai",
        "model": "configured-model",
        "api_key_env": "OPENAI_API_KEY",
    }

    resolved = resolve_llm_payload({"llm": raw_config})

    assert resolved == raw_config
    assert resolved is not raw_config


def test_cli_rejects_non_object_llm_payload() -> None:
    """CLI 请求信封中的 llm 必须是对象或 null。"""
    with pytest.raises(ValueError, match="llm 必须是对象"):
        resolve_llm_payload({"llm": ["openai"]})


@pytest.mark.parametrize(
    "field_name, invalid_value",
    [
        ("temperature", float("nan")),
        ("temperature", 2.1),
        ("max_output_tokens", 0),
        ("timeout_seconds", 0),
        ("timeout_seconds", 301),
    ],
)
def test_llm_config_rejects_invalid_numeric_bounds(
    field_name: str,
    invalid_value: object,
) -> None:
    """LLM 数值配置必须拒绝非有限值、零值和超过安全上限的值。

    Args:
        field_name: 当前参数化覆盖的 LLM 配置字段名称。
        invalid_value: 当前字段需要拒绝的配置值。
    """
    with pytest.raises((TypeError, ValueError), match=field_name):
        create_llm_config_state({field_name: invalid_value})
