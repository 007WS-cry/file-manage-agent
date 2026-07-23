from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.llm.config import create_llm_config_state
from app.llm.model_profiles import resolve_model_profile
from app.llm.providers.base import LLMProviderConfigurationError
from app.llm.providers.langchain import (
    SUPPORTED_LANGCHAIN_PROVIDERS,
    LangChainChatModelProvider,
    _create_langchain_chat_model,
)
from app.state.models import ContentSubagentOutput

"""本模块验证模型 Profile 兼容转换、任务路由和 LangChain 结构化适配边界。"""


class FakeStructuredRunnable:
    """模拟 ``with_structured_output`` 返回的 LangChain Runnable。"""

    def __init__(self, output_model: type[ContentSubagentOutput]) -> None:
        """记录目标输出类型并创建空调用记录。

        Args:
            output_model: LangChain 结构化调用要求的 Pydantic 类型。
        """
        self.output_model = output_model
        # 本次 Runnable 必须返回的 Pydantic 类型。

        self.last_messages: list[tuple[str, str]] | None = None
        # 最近一次 ``invoke`` 接收的最小消息列表。

    def invoke(self, messages: list[tuple[str, str]]) -> dict[str, object]:
        """返回包含原始用量和已解析 Pydantic 对象的模拟响应。

        Args:
            messages: LangChain 适配器发送的 System 与 Human 消息。

        Returns:
            与 ``include_raw=True`` 协议一致的模拟结构化响应。
        """
        self.last_messages = list(messages)
        return {
            "raw": SimpleNamespace(
                usage_metadata={
                    "input_tokens": 17,
                    "output_tokens": 6,
                    "total_tokens": 23,
                }
            ),
            "parsed": self.output_model(
                summary="LangChain Profile 路由测试摘要。",
                artifact_refs=["artifact://document/1"],
            ),
            "parsing_error": None,
        }


class FakeChatModel:
    """模拟支持 Pydantic 结构化输出的 LangChain Chat Model。"""

    def __init__(self) -> None:
        """创建尚未绑定结构化输出的模拟 Chat Model。"""
        self.structured_runnable: FakeStructuredRunnable | None = None
        # 最近一次创建的结构化 Runnable。

        self.structured_options: dict[str, object] = {}
        # 最近一次 ``with_structured_output`` 接收的关键字参数。

    def with_structured_output(
        self,
        output_model: type[ContentSubagentOutput],
        *,
        include_raw: bool,
        **options: object,
    ) -> FakeStructuredRunnable:
        """记录结构化输出类型并返回可调用 Runnable。

        Args:
            output_model: Provider 要求模型遵守的 Pydantic 类型。
            include_raw: 是否同时返回携带 Token 用量的原始消息。
            options: Provider 专有的可选结构化输出参数。

        Returns:
            测试专用结构化 Runnable。
        """
        assert include_raw is True
        self.structured_options = dict(options)
        runnable = FakeStructuredRunnable(output_model)
        self.structured_runnable = runnable
        return runnable


def test_legacy_single_model_config_becomes_default_profile() -> None:
    """0.5.1 单模型配置应转换为默认 Profile 并保留兼容镜像字段。"""
    config = create_llm_config_state(
        {
            "enabled": True,
            "provider": "openai",
            "model": "legacy-model",
            "api_key_env": "LEGACY_OPENAI_API_KEY",
            "temperature": 0.1,
            "max_output_tokens": 256,
            "timeout_seconds": 12.5,
        }
    )

    assert config["default_profile_id"] == "default"
    assert config["task_profile_ids"] == {}
    assert config["profiles"] == [
        {
            "id": "default",
            "provider": "openai",
            "model": "legacy-model",
                "api_key_env": "LEGACY_OPENAI_API_KEY",
                "base_url_env": "OPENAI_BASE_URL",
                "options_env": None,
                "structured_output_method": "auto",
                "temperature": 0.1,
            "max_output_tokens": 256,
            "timeout_seconds": 12.5,
        }
    ]
    assert config["provider"] == config["profiles"][0]["provider"]
    assert config["model"] == config["profiles"][0]["model"]


@pytest.mark.parametrize(
    ("declared_provider", "canonical_provider", "api_key_env", "base_url_env"),
    [
        ("claude", "anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
        ("gemini", "google_genai", "GOOGLE_API_KEY", None),
        ("deepseek", "deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE"),
        ("qwen", "qwen", "DASHSCOPE_API_KEY", "DASHSCOPE_API_BASE"),
        ("glm", "zhipuai", "ZHIPUAI_API_KEY", "ZHIPUAI_BASE_URL"),
        (
            "openai-compatible",
            "openai_compatible",
            "OPENAI_COMPATIBLE_API_KEY",
            "OPENAI_COMPATIBLE_BASE_URL",
        ),
        ("openrouter", "openrouter", "OPENROUTER_API_KEY", None),
        ("litellm", "litellm", None, None),
    ],
)
def test_mainstream_provider_aliases_receive_safe_defaults(
    declared_provider: str,
    canonical_provider: str,
    api_key_env: str | None,
    base_url_env: str | None,
) -> None:
    """主流模型和路由服务应规范化名称并只把环境变量名称写入状态。"""
    config = create_llm_config_state(
        {
            "enabled": True,
            "provider": declared_provider,
            "model": "provider-model",
        }
    )

    profile = config["profiles"][0]
    assert profile["provider"] == canonical_provider
    assert profile["api_key_env"] == api_key_env
    assert profile["base_url_env"] == base_url_env
    assert profile["options_env"] is None
    assert profile["structured_output_method"] == "auto"


def test_every_registered_langchain_provider_is_accepted_by_profile() -> None:
    """LangChain 内置主流 Provider 和项目扩展入口都应可进入统一 Profile。"""
    accepted = {
        create_llm_config_state(
            {
                "provider": provider_name,
                "model": "provider-model",
            }
        )["provider"]
        for provider_name in SUPPORTED_LANGCHAIN_PROVIDERS
    }

    assert accepted == set(SUPPORTED_LANGCHAIN_PROVIDERS)


@pytest.mark.parametrize(
    "provider_name",
    [
        "anthropic",
        "deepseek",
        "google_genai",
        "litellm",
        "openrouter",
    ],
)
def test_builtin_mainstream_provider_uses_langchain_factory(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    """原生主流 Provider 应把模型名和规范名称交给 LangChain 统一工厂。"""
    captured: dict[str, object] = {}
    expected_model = object()

    def fake_init_chat_model(
        model: str,
        *,
        model_provider: str,
        **options: object,
    ) -> object:
        """记录 LangChain 工厂调用而不导入或连接真实 Provider。

        Args:
            model: 当前 Profile 的模型名称。
            model_provider: LangChain 规范 Provider 名称。
            options: 其余已校验构造参数。

        Returns:
            用于确认工厂返回值透传的唯一对象。
        """
        captured.update(
            {
                "model": model,
                "model_provider": model_provider,
                **options,
            }
        )
        return expected_model

    monkeypatch.setattr(
        "langchain.chat_models.init_chat_model",
        fake_init_chat_model,
    )

    result = _create_langchain_chat_model(
        provider_name=provider_name,
        model="provider-model",
        temperature=0.0,
    )

    assert result is expected_model
    assert captured == {
        "model": "provider-model",
        "model_provider": provider_name,
        "temperature": 0.0,
    }


def test_qwen_provider_uses_chat_qwen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qwen Profile 应调用 ``langchain-qwq`` 提供的 ChatQwen。"""
    captured: dict[str, object] = {}
    expected_model = object()

    def fake_chat_qwen(**options: object) -> object:
        """记录 ChatQwen 构造参数并返回唯一测试对象。

        Args:
            options: Qwen Chat Model 的构造参数。

        Returns:
            用于确认专用工厂分支的唯一对象。
        """
        captured.update(options)
        return expected_model

    monkeypatch.setitem(
        sys.modules,
        "langchain_qwq",
        SimpleNamespace(ChatQwen=fake_chat_qwen),
    )

    result = _create_langchain_chat_model(
        provider_name="qwen",
        model="qwen-flash",
        api_key="secret",
    )

    assert result is expected_model
    assert captured == {"model": "qwen-flash", "api_key": "secret"}


@pytest.mark.parametrize("provider_name", ["openai_compatible", "zhipuai"])
def test_openai_compatible_provider_uses_chat_openai(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    """通用中转站与 GLM 应通过 ChatOpenAI 连接显式兼容端点。"""
    captured: dict[str, object] = {}
    expected_model = object()

    def fake_chat_openai(**options: object) -> object:
        """记录兼容端点构造参数并返回唯一测试对象。

        Args:
            options: ChatOpenAI 的模型、凭据和 Base URL 参数。

        Returns:
            用于确认兼容工厂分支的唯一对象。
        """
        captured.update(options)
        return expected_model

    monkeypatch.setattr("langchain_openai.ChatOpenAI", fake_chat_openai)

    result = _create_langchain_chat_model(
        provider_name=provider_name,
        model="compatible-model",
        api_key="secret",
        base_url="https://relay.example.test/v1",
    )

    assert result is expected_model
    assert captured == {
        "model": "compatible-model",
        "api_key": "secret",
        "base_url": "https://relay.example.test/v1",
    }


def test_multi_model_config_routes_each_fixed_task_type() -> None:
    """多模型配置应按 Content、Version 和 Evidence 固定任务类型选择 Profile。"""
    config = create_llm_config_state(
        {
            "enabled": True,
            "profiles": [
                {
                    "id": "fast",
                    "provider": "openai",
                    "model": "gpt-fast",
                    "api_key_env": "OPENAI_API_KEY",
                },
                {
                    "id": "deep",
                    "provider": "openai",
                    "model": "gpt-deep",
                    "api_key_env": "OPENAI_API_KEY",
                    "max_output_tokens": 1600,
                },
            ],
            "default_profile_id": "fast",
            "task_profile_ids": {
                "content": "fast",
                "version": "deep",
                "evidence": "fast",
            },
            "fallback_enabled": True,
        }
    )

    assert resolve_model_profile(config, task_type="content")["model"] == "gpt-fast"
    assert resolve_model_profile(config, task_type="version")["model"] == "gpt-deep"
    assert resolve_model_profile(config, task_type="evidence")["id"] == "fast"
    assert resolve_model_profile(config)["id"] == "fast"


def test_model_profiles_reject_duplicates_and_unknown_routes() -> None:
    """Profile ID 重复或任务路由引用不存在时应在建图前失败。"""
    duplicate_profiles = [
        {"id": "same", "provider": "mock", "model": "mock-a"},
        {"id": "same", "provider": "mock", "model": "mock-b"},
    ]
    with pytest.raises(ValueError, match="重复 ID"):
        create_llm_config_state({"profiles": duplicate_profiles})

    with pytest.raises(ValueError, match="不存在的 Profile"):
        create_llm_config_state(
            {
                "profiles": [
                    {"id": "only", "provider": "mock", "model": "mock-only"}
                ],
                "task_profile_ids": {"version": "missing"},
            }
        )


def test_multi_model_config_rejects_conflicting_legacy_mirrors() -> None:
    """多 Profile 配置若同时声明旧字段，其值必须与默认 Profile 保持一致。"""
    with pytest.raises(ValueError, match="旧兼容字段"):
        create_llm_config_state(
            {
                "profiles": [
                    {
                        "id": "default",
                        "provider": "mock",
                        "model": "mock-profile",
                    }
                ],
                "default_profile_id": "default",
                "model": "conflicting-legacy-model",
            }
        )


def test_model_profile_never_accepts_secret_or_base_url_actual_value() -> None:
    """Profile 只能保存环境变量名称，直接凭据或服务地址字段必须被拒绝。"""
    with pytest.raises(ValueError, match="未知字段"):
        create_llm_config_state(
            {
                "profiles": [
                    {
                        "id": "unsafe",
                        "provider": "openai",
                        "model": "gpt-test",
                        "api_key_env": "OPENAI_API_KEY",
                        "api_key": "secret-must-not-enter-state",
                    }
                ]
            }
        )

    with pytest.raises(ValueError, match="未知字段"):
        create_llm_config_state(
            {
                "profiles": [
                    {
                        "id": "unsafe",
                        "provider": "openai",
                        "model": "gpt-test",
                        "api_key_env": "OPENAI_API_KEY",
                        "base_url": "https://private.example.test/v1",
                    }
                ]
            }
        )


def test_langchain_provider_uses_profile_options_and_extracts_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LangChain 适配器应传递 Profile 参数并保留 Pydantic 输出和 Token 用量。"""
    captured_options: dict[str, object] = {}
    chat_model = FakeChatModel()

    def create_chat_model(**options: object) -> FakeChatModel:
        """记录 Chat Model 构造参数并返回无网络模拟模型。

        Args:
            options: LangChain 适配器传给模型工厂的 Profile 参数。

        Returns:
            测试专用 Chat Model。
        """
        captured_options.update(options)
        return chat_model

    monkeypatch.setenv("PROFILE_TEST_API_KEY", "secret-not-stored")
    monkeypatch.setenv("PROFILE_TEST_BASE_URL", "https://relay.example.test/v1")
    provider = LangChainChatModelProvider(
        provider_name="openai",
        api_key_env="PROFILE_TEST_API_KEY",
        base_url_env="PROFILE_TEST_BASE_URL",
        model_factory=create_chat_model,
    )

    response = provider.generate_structured(
        model="profile-model",
        system_prompt="你是只读内容分析助手。",
        user_prompt="只解释给定摘要。",
        output_model=ContentSubagentOutput,
        temperature=0.2,
        max_output_tokens=300,
        timeout_seconds=9.5,
    )

    assert response.output.summary == "LangChain Profile 路由测试摘要。"
    assert response.total_tokens == 23
    assert captured_options == {
        "model": "profile-model",
        "api_key": "secret-not-stored",
        "temperature": 0.2,
        "max_tokens": 300,
        "timeout": 9.5,
        "max_retries": 0,
        "base_url": "https://relay.example.test/v1",
    }
    assert chat_model.structured_runnable is not None
    assert chat_model.structured_runnable.last_messages == [
        ("system", "你是只读内容分析助手。"),
        ("human", "只解释给定摘要。"),
    ]
    assert "secret-not-stored" not in repr(provider)


def test_langchain_provider_reads_structured_method_and_private_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider 专有参数和结构化方法应从受控配置传入且不进入状态。"""
    captured_options: dict[str, object] = {}
    chat_model = FakeChatModel()

    def create_chat_model(**options: object) -> FakeChatModel:
        """记录包含路由专有字段的构造参数并返回模拟模型。

        Args:
            options: 从 Profile 公共字段和环境 JSON 合并出的构造参数。

        Returns:
            测试专用 Chat Model。
        """
        captured_options.update(options)
        return chat_model

    monkeypatch.setenv("ROUTER_API_KEY", "router-secret")
    monkeypatch.setenv("ROUTER_BASE_URL", "https://relay.example.test/v1")
    monkeypatch.setenv(
        "ROUTER_OPTIONS",
        '{"default_headers":{"X-Tenant":"tenant-a"},"extra_body":{"route":"fast"}}',
    )
    provider = LangChainChatModelProvider(
        provider_name="openai_compatible",
        api_key_env="ROUTER_API_KEY",
        base_url_env="ROUTER_BASE_URL",
        options_env="ROUTER_OPTIONS",
        structured_output_method="function_calling",
        model_factory=create_chat_model,
    )

    provider.generate_structured(
        model="relay-model",
        system_prompt="你是只读内容分析助手。",
        user_prompt="只解释给定摘要。",
        output_model=ContentSubagentOutput,
        temperature=0.0,
        max_output_tokens=200,
        timeout_seconds=8.0,
    )

    assert captured_options["default_headers"] == {"X-Tenant": "tenant-a"}
    assert captured_options["extra_body"] == {"route": "fast"}
    assert chat_model.structured_options == {"method": "function_calling"}
    assert "router-secret" not in repr(provider)


def test_langchain_provider_rejects_reserved_private_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境 JSON 不得绕过 Profile 校验覆盖模型、凭据或请求预算。"""
    monkeypatch.setenv("UNSAFE_PROVIDER_OPTIONS", '{"model":"shadow-model"}')
    provider = LangChainChatModelProvider(
        provider_name="ollama",
        options_env="UNSAFE_PROVIDER_OPTIONS",
        model_factory=lambda **options: FakeChatModel(),
    )

    with pytest.raises(LLMProviderConfigurationError, match="不得覆盖 model"):
        provider.generate_structured(
            model="safe-model",
            system_prompt="你是只读内容分析助手。",
            user_prompt="只解释给定摘要。",
            output_model=ContentSubagentOutput,
            temperature=0.0,
            max_output_tokens=200,
            timeout_seconds=8.0,
        )


def test_compatible_provider_requires_runtime_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容 Provider 缺少实际端点时不得误把请求发送到 OpenAI 官方地址。"""
    monkeypatch.setenv("COMPATIBLE_API_KEY", "relay-secret")
    monkeypatch.delenv("MISSING_COMPATIBLE_BASE_URL", raising=False)
    provider = LangChainChatModelProvider(
        provider_name="openai_compatible",
        api_key_env="COMPATIBLE_API_KEY",
        base_url_env="MISSING_COMPATIBLE_BASE_URL",
        model_factory=lambda **options: FakeChatModel(),
    )

    with pytest.raises(LLMProviderConfigurationError, match="Base URL"):
        provider.generate_structured(
            model="relay-model",
            system_prompt="你是只读内容分析助手。",
            user_prompt="只解释给定摘要。",
            output_model=ContentSubagentOutput,
            temperature=0.0,
            max_output_tokens=200,
            timeout_seconds=8.0,
        )


def test_missing_optional_provider_dependency_has_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未安装可选 Provider 包时应返回包名明确且不包含凭据的配置错误。"""

    def missing_provider(**options: object) -> object:
        """模拟可选 LangChain Provider 包尚未安装。

        Args:
            options: 本次模型构造参数。

        Raises:
            ImportError: 始终模拟缺少 Provider 包。
        """
        raise ImportError("simulated optional dependency")

    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "secret-not-in-error")
    provider = LangChainChatModelProvider(
        provider_name="anthropic",
        api_key_env="ANTHROPIC_TEST_KEY",
        model_factory=missing_provider,
    )

    with pytest.raises(
        LLMProviderConfigurationError,
        match="langchain-anthropic",
    ) as captured:
        provider.generate_structured(
            model="claude-test",
            system_prompt="你是只读内容分析助手。",
            user_prompt="只解释给定摘要。",
            output_model=ContentSubagentOutput,
            temperature=0.0,
            max_output_tokens=200,
            timeout_seconds=8.0,
        )
    assert "secret-not-in-error" not in str(captured.value)
