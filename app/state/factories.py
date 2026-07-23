from __future__ import annotations

import os
import sysconfig
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from app.llm.config import create_llm_config_state
from app.services.context_compaction import (
    DEFAULT_CONTEXT_COMPACT_TRIGGER_TOKENS,
    DEFAULT_RETAINED_PREVIEW_CHARACTERS,
)
from app.services.memory_policy import (
    derive_configured_memory_namespace,
    derive_memory_namespace,
)
from app.skills.loader import create_pending_skill_registry
from app.state.models import (
    AgentMemberState,
    ApplicationDatabaseState,
    ContextCompactState,
    FileGovernanceState,
    HookConfigState,
    MemoryState,
    PromptState,
    RequestState,
    TeamState,
    WorkspaceState,
)
from app.storage.database import (
    DEFAULT_APPLICATION_DATABASE_PATH,
    validate_application_database_path,
)

"""本模块负责定位默认受控 Prompt，并创建可提交给顶层 LangGraph 的初始状态。"""

# 默认 System Prompt 版本，与仓库中的受控 Prompt 资源保持一致。
DEFAULT_PROMPT_VERSION = "file-governance-v1"


def _resolve_default_prompt_source_path() -> str:
    """解析源码目录或安装数据目录中的默认 Prompt 资源路径。

    开发和容器环境优先使用仓库根目录下的受控资源；wheel 安装不包含源码根目录时，
    回退到 setuptools ``data-files`` 安装到 Python 数据前缀的同名资源。函数只进行
    本地路径定位，不读取 Prompt 内容，也不执行文件中的任何文本。

    Returns:
        默认 Prompt 资源的绝对路径；文件存在性和安全性由加载节点继续校验。
    """
    relative_path = Path("resources/prompts/file_governance_system_v1.md")
    source_path = Path(__file__).resolve().parents[2] / relative_path
    if source_path.is_file():
        return str(source_path)
    installed_path = Path(sysconfig.get_path("data")) / relative_path
    return str(installed_path)


# 默认 System Prompt 资源路径，兼容源码、容器和 wheel 安装布局。
DEFAULT_PROMPT_SOURCE_PATH = _resolve_default_prompt_source_path()

# 0.4.4 三个业务阶段和固定 Subagent 共用的 Team Protocol 版本。
DEFAULT_TEAM_PROTOCOL_VERSION = "team-protocol-v1"

# 0.4.4 固定团队允许的最大 Subagent 并发数；当前编排图仍按单请求串行调用。
DEFAULT_MAX_PARALLEL_AGENTS = 3

# 每次新运行默认最多召回的长期 Memory 条目数量。
DEFAULT_MEMORY_RECALL_LIMIT = 50

# 可覆盖长期 Memory 默认应用数据库位置的环境变量名称，与 Alembic 保持一致。
APPLICATION_DATABASE_PATH_ENV = "FILE_GOVERNANCE_DATABASE_PATH"


def create_disabled_application_database_state() -> ApplicationDatabaseState:
    """创建不会打开连接或产生持久化副作用的应用数据库状态。

    Returns:
        路径为空、状态为 ``disabled`` 的 SQLite 应用数据库配置。
    """
    return ApplicationDatabaseState(
        enabled=False,
        backend="sqlite",
        database_path=None,
        checkpoint_path=None,
        auto_create_parent=True,
        echo=False,
        timeout_seconds=30.0,
        status="disabled",
        last_error=None,
    )


def copy_application_database_state(
    application_database: Mapping[str, object] | None,
) -> ApplicationDatabaseState:
    """复制应用数据库状态，并为 0.5.0 checkpoint 补齐关闭默认值。

    Args:
        application_database: 当前顶层状态中的可选应用数据库对象。

    Returns:
        与输入解除引用关系的完整应用数据库状态。
    """
    if application_database is None:
        return create_disabled_application_database_state()
    enabled = bool(application_database.get("enabled", False))
    raw_status = application_database.get(
        "status",
        "pending" if enabled else "disabled",
    )
    status = raw_status if raw_status in {"disabled", "pending", "ready", "failed"} else "failed"
    return ApplicationDatabaseState(
        enabled=enabled,
        backend="sqlite",
        database_path=(
            str(application_database["database_path"])
            if application_database.get("database_path") is not None
            else None
        ),
        checkpoint_path=(
            str(application_database["checkpoint_path"])
            if application_database.get("checkpoint_path") is not None
            else None
        ),
        auto_create_parent=bool(application_database.get("auto_create_parent", True)),
        echo=bool(application_database.get("echo", False)),
        timeout_seconds=float(application_database.get("timeout_seconds", 30.0)),
        status=cast(
            Literal["disabled", "pending", "ready", "failed"],
            status,
        ),
        last_error=(
            str(application_database["last_error"])
            if application_database.get("last_error") is not None
            else None
        ),
    )


def _normalize_string_list(value: object, *, field_name: str) -> list[str]:
    """复制并校验配置中的字符串列表。

    Args:
        value: 等待校验的配置值。
        field_name: 用于错误信息的字段名称。

    Returns:
        与调用方输入解除可变引用关系的字符串列表。

    Raises:
        TypeError: 配置值不是列表或列表元素不是字符串时抛出。
        ValueError: 列表包含空字符串或重复值时抛出。
    """
    if not isinstance(value, list):
        raise TypeError(f"{field_name} 必须是字符串列表")

    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{field_name} 的元素必须是字符串")
        name = item.strip()
        if not name:
            raise ValueError(f"{field_name} 不得包含空字符串")
        if name in normalized:
            raise ValueError(f"{field_name} 不得包含重复值：{name}")
        normalized.append(name)
    return normalized


def _reject_unknown_fields(
    config: Mapping[str, object],
    *,
    allowed_fields: set[str],
    config_name: str,
) -> None:
    """拒绝配置对象中的未知字段，避免拼写错误被静默忽略。

    Args:
        config: 等待检查的配置映射。
        allowed_fields: 当前协议允许的字段名称集合。
        config_name: 用于错误信息的配置对象名称。

    Raises:
        ValueError: 配置包含协议未定义的字段时抛出。
    """
    unknown_fields = sorted(set(config) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"{config_name} 包含未知字段：{', '.join(unknown_fields)}")


def create_prompt_state(
    prompt_config: Mapping[str, object] | None = None,
) -> PromptState:
    """根据可选配置创建尚未加载正文的 System Prompt 状态。

    Args:
        prompt_config: 可选 Prompt 配置；省略时完全关闭 System Prompt。

    Returns:
        状态为 ``pending`` 或 ``disabled`` 的独立 Prompt 状态对象。

    Raises:
        TypeError: 布尔值、版本、路径或动态规则类型不正确时抛出。
        ValueError: 配置包含未知字段、空版本或启用时缺少资源路径时抛出。
    """
    config = dict(prompt_config or {})
    _reject_unknown_fields(
        config,
        allowed_fields={"enabled", "version", "source_path", "dynamic_rules"},
        config_name="prompt_config",
    )

    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TypeError("prompt_config.enabled 必须是布尔值")

    version = config.get("version", DEFAULT_PROMPT_VERSION)
    if not isinstance(version, str):
        raise TypeError("prompt_config.version 必须是字符串")
    version = version.strip()
    if not version:
        raise ValueError("prompt_config.version 不得为空")

    raw_source_path = config.get("source_path", DEFAULT_PROMPT_SOURCE_PATH)
    if raw_source_path is not None and not isinstance(raw_source_path, str):
        raise TypeError("prompt_config.source_path 必须是路径字符串或 null")
    normalized_source_path = raw_source_path.strip() if raw_source_path else None
    if enabled and normalized_source_path is None:
        raise ValueError("启用 System Prompt 时必须提供 source_path")

    dynamic_rules = _normalize_string_list(
        config.get("dynamic_rules", []),
        field_name="prompt_config.dynamic_rules",
    )
    return PromptState(
        enabled=enabled,
        version=version,
        source_path=normalized_source_path if enabled else None,
        content="",
        content_sha256=None,
        dynamic_rules=dynamic_rules,
        status="pending" if enabled else "disabled",
    )


def create_hook_config_state(
    hook_config: Mapping[str, object] | None = None,
) -> HookConfigState:
    """根据可选配置创建生命周期 Hooks 状态。

    Args:
        hook_config: 可选 Hook 配置；省略时完全关闭所有生命周期 Hook。

    Returns:
        已复制执行列表并校验失败策略的 Hook 配置状态。

    Raises:
        TypeError: 开关、Hook 列表或失败策略映射类型不正确时抛出。
        ValueError: 配置包含未知字段、重复 Hook 或未知失败策略时抛出。
    """
    config = dict(hook_config or {})
    _reject_unknown_fields(
        config,
        allowed_fields={
            "enabled",
            "before_run",
            "before_model",
            "after_model",
            "after_run",
            "default_failure_policy",
            "failure_policies",
        },
        config_name="hook_config",
    )

    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TypeError("hook_config.enabled 必须是布尔值")

    raw_default_failure_policy = config.get("default_failure_policy", "block")
    if not isinstance(raw_default_failure_policy, str):
        raise TypeError("hook_config.default_failure_policy 必须是字符串")
    if raw_default_failure_policy not in {"block", "ignore"}:
        raise ValueError("hook_config.default_failure_policy 只能是 block 或 ignore")
    default_failure_policy = cast(
        Literal["block", "ignore"],
        raw_default_failure_policy,
    )

    raw_failure_policies = config.get("failure_policies", {})
    if not isinstance(raw_failure_policies, Mapping):
        raise TypeError("hook_config.failure_policies 必须是对象")
    failure_policies: dict[str, Literal["block", "ignore"]] = {}
    for raw_name, raw_policy in raw_failure_policies.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("hook_config.failure_policies 的 Hook 名称不得为空")
        if not isinstance(raw_policy, str):
            raise TypeError(f"Hook {raw_name} 的失败策略必须是字符串")
        if raw_policy not in {"block", "ignore"}:
            raise ValueError(f"Hook {raw_name} 的失败策略只能是 block 或 ignore")
        normalized_name = raw_name.strip()
        if normalized_name in failure_policies:
            raise ValueError(f"failure_policies 不得包含重复 Hook：{normalized_name}")
        failure_policies[normalized_name] = cast(
            Literal["block", "ignore"],
            raw_policy,
        )

    return HookConfigState(
        enabled=enabled,
        before_run=_normalize_string_list(
            config.get("before_run", []),
            field_name="hook_config.before_run",
        ),
        before_model=_normalize_string_list(
            config.get("before_model", []),
            field_name="hook_config.before_model",
        ),
        after_model=_normalize_string_list(
            config.get("after_model", []),
            field_name="hook_config.after_model",
        ),
        after_run=_normalize_string_list(
            config.get("after_run", []),
            field_name="hook_config.after_run",
        ),
        default_failure_policy=default_failure_policy,
        failure_policies=failure_policies,
    )


def create_team_state() -> TeamState:
    """创建协调者和三个固定角色组成的初始 Agent Team 状态。

    状态工厂只建立稳定成员、职责和协议状态，不创建模型 Client、执行 Subagent
    或分配业务 Task，因而不会产生网络、文件或其他外部副作用。

    Returns:
        四个成员均为空闲、没有当前 Task 且 Skills 为空的固定 Team 状态。
    """
    members = [
        AgentMemberState(
            id="coordinator-agent",
            role="coordinator",
            status="idle",
            current_task_id=None,
            tool_names=[],
            skill_ids=[],
        ),
        AgentMemberState(
            id="content-subagent",
            role="content",
            status="idle",
            current_task_id=None,
            tool_names=[],
            skill_ids=[],
        ),
        AgentMemberState(
            id="version-subagent",
            role="version",
            status="idle",
            current_task_id=None,
            tool_names=[],
            skill_ids=[],
        ),
        AgentMemberState(
            id="evidence-subagent",
            role="evidence",
            status="idle",
            current_task_id=None,
            tool_names=[],
            skill_ids=[],
        ),
    ]
    return TeamState(
        coordinator_id="coordinator-agent",
        members=members,
        protocol_version=DEFAULT_TEAM_PROTOCOL_VERSION,
        max_parallel_agents=DEFAULT_MAX_PARALLEL_AGENTS,
    )


def create_memory_state(
    request: RequestState,
    memory_config: Mapping[str, object] | None = None,
    *,
    checkpoint_path: str | Path | None = None,
) -> MemoryState:
    """根据可选配置创建默认关闭、按工作空间隔离的 Memory 状态。

    显式启用时，命名空间只保存目录或调用方种子的哈希，数据库路径必须位于
    只读输入目录之外。此工厂不创建目录、数据库文件或数据表。

    Args:
        request: 包含输入根目录的治理请求。
        memory_config: 可选 Memory 开关、命名空间种子、数据库路径和召回上限。
        checkpoint_path: 可选 SQLite Checkpointer 路径，用于数据库文件隔离校验。

    Returns:
        缓冲区为空且状态为 ``pending`` 或 ``disabled`` 的 Memory 状态。

    Raises:
        TypeError: 配置字段类型不符合协议时抛出。
        ValueError: 配置包含未知字段、非法上限或不安全数据库路径时抛出。
    """
    config = dict(memory_config or {})
    _reject_unknown_fields(
        config,
        allowed_fields={"enabled", "namespace", "database_path", "recall_limit"},
        config_name="memory_config",
    )

    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TypeError("memory_config.enabled 必须是布尔值")

    raw_namespace = config.get("namespace")
    if raw_namespace is not None and not isinstance(raw_namespace, str):
        raise TypeError("memory_config.namespace 必须是字符串或 null")
    if isinstance(raw_namespace, str) and not raw_namespace.strip():
        raise ValueError("memory_config.namespace 不得为空")
    namespace = (
        derive_configured_memory_namespace(raw_namespace)
        if isinstance(raw_namespace, str)
        else derive_memory_namespace(request["root_directory"])
    )
    if not enabled:
        namespace = ""

    raw_database_path = config.get(
        "database_path",
        os.environ.get(
            APPLICATION_DATABASE_PATH_ENV,
            str(DEFAULT_APPLICATION_DATABASE_PATH),
        ),
    )
    if not isinstance(raw_database_path, (str, Path)):
        raise TypeError("memory_config.database_path 必须是字符串或 Path")
    database_path = (
        str(
            validate_application_database_path(
                raw_database_path,
                input_root=request["root_directory"],
                checkpoint_path=checkpoint_path,
            )
        )
        if enabled
        else None
    )

    recall_limit = config.get("recall_limit", DEFAULT_MEMORY_RECALL_LIMIT)
    if isinstance(recall_limit, bool) or not isinstance(recall_limit, int):
        raise TypeError("memory_config.recall_limit 必须是整数")
    if recall_limit < 1 or recall_limit > 1000:
        raise ValueError("memory_config.recall_limit 必须位于 1 到 1000 之间")

    return MemoryState(
        enabled=enabled,
        namespace=namespace,
        database_path=database_path,
        checkpoint_path=(
            str(Path(checkpoint_path).expanduser().resolve())
            if enabled and checkpoint_path is not None
            else None
        ),
        recall_limit=recall_limit,
        status="pending" if enabled else "disabled",
        recalled_items=[],
        short_term_items=[],
        pending_long_term_items=[],
        persisted_item_ids=[],
        last_error=None,
    )


def create_context_compact_state(
    request: RequestState,
    context_compact_config: Mapping[str, object] | None = None,
    *,
    checkpoint_path: str | Path | None = None,
) -> ContextCompactState:
    """根据可选配置创建默认关闭的 Context Compact 状态。

    Args:
        request: 包含只读输入根目录的治理请求。
        context_compact_config: 可选压缩开关、阈值、预览保留量和数据库配置。
        checkpoint_path: 可选 SQLite Checkpointer 路径，用于数据库隔离校验。

    Returns:
        尚未估算上下文且摘要列表为空的 Context Compact 状态。

    Raises:
        TypeError: 配置字段类型不符合协议时抛出。
        ValueError: 配置包含未知字段、数值越界或数据库路径不安全时抛出。
    """
    config = dict(context_compact_config or {})
    _reject_unknown_fields(
        config,
        allowed_fields={
            "enabled",
            "trigger_token_threshold",
            "retained_preview_characters",
            "persist_summaries",
            "database_path",
        },
        config_name="context_compact_config",
    )
    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TypeError("context_compact_config.enabled 必须是布尔值")
    persist_summaries = config.get("persist_summaries", True)
    if not isinstance(persist_summaries, bool):
        raise TypeError("context_compact_config.persist_summaries 必须是布尔值")

    trigger_token_threshold = config.get(
        "trigger_token_threshold",
        DEFAULT_CONTEXT_COMPACT_TRIGGER_TOKENS,
    )
    if isinstance(trigger_token_threshold, bool) or not isinstance(trigger_token_threshold, int):
        raise TypeError("context_compact_config.trigger_token_threshold 必须是整数")
    if trigger_token_threshold < 1 or trigger_token_threshold > 10_000_000:
        raise ValueError(
            "context_compact_config.trigger_token_threshold 必须位于 1 到 10000000 之间"
        )

    retained_preview_characters = config.get(
        "retained_preview_characters",
        DEFAULT_RETAINED_PREVIEW_CHARACTERS,
    )
    if isinstance(retained_preview_characters, bool) or not isinstance(
        retained_preview_characters, int
    ):
        raise TypeError("context_compact_config.retained_preview_characters 必须是整数")
    if retained_preview_characters < 0 or retained_preview_characters > 1000:
        raise ValueError(
            "context_compact_config.retained_preview_characters 必须位于 0 到 1000 之间"
        )

    raw_database_path = config.get(
        "database_path",
        os.environ.get(
            APPLICATION_DATABASE_PATH_ENV,
            str(DEFAULT_APPLICATION_DATABASE_PATH),
        ),
    )
    if not isinstance(raw_database_path, (str, Path)):
        raise TypeError("context_compact_config.database_path 必须是字符串或 Path")
    use_database = enabled and persist_summaries
    database_path = (
        str(
            validate_application_database_path(
                raw_database_path,
                input_root=request["root_directory"],
                checkpoint_path=checkpoint_path,
            )
        )
        if use_database
        else None
    )
    return ContextCompactState(
        enabled=enabled,
        trigger_token_threshold=trigger_token_threshold,
        retained_preview_characters=retained_preview_characters,
        persist_summaries=persist_summaries if enabled else False,
        database_path=database_path,
        checkpoint_path=(
            str(Path(checkpoint_path).expanduser().resolve())
            if use_database and checkpoint_path is not None
            else None
        ),
        status="pending" if enabled else "disabled",
        current_stage=None,
        estimated_tokens=0,
        summaries=[],
        last_error=None,
    )


def create_application_database_state(
    request: RequestState,
    application_database_config: Mapping[str, object] | None = None,
    *,
    memory: MemoryState | None = None,
    context_compact: ContextCompactState | None = None,
    checkpoint_path: str | Path | None = None,
) -> ApplicationDatabaseState:
    """创建与 Checkpointer 隔离、并供五张应用表共同使用的数据库状态。

    Memory 或 Context Summary 已启用时会自动启用本状态，并要求三者使用同一个
    SQLite 文件。只启用运行历史或工具审计时，可以单独传入
    ``application_database_config.enabled=true``。

    Args:
        request: 包含只读输入目录的治理请求。
        application_database_config: 可选后端、路径、日志和锁等待配置。
        memory: 已创建的 Memory 状态，用于统一数据库文件。
        context_compact: 已创建的 Context Compact 状态，用于统一数据库文件。
        checkpoint_path: 可选 SQLite Checkpointer 路径，用于文件隔离校验。

    Returns:
        状态为 ``pending`` 或 ``disabled`` 的应用数据库配置。

    Raises:
        TypeError: 配置字段类型不符合协议时抛出。
        ValueError: 后端、路径、超时或数据库隔离配置不安全时抛出。
    """
    config = dict(application_database_config or {})
    _reject_unknown_fields(
        config,
        allowed_fields={
            "enabled",
            "backend",
            "database_path",
            "auto_create_parent",
            "echo",
            "timeout_seconds",
        },
        config_name="application_database_config",
    )
    configured_enabled = config.get("enabled", False)
    if not isinstance(configured_enabled, bool):
        raise TypeError("application_database_config.enabled 必须是布尔值")
    backend = config.get("backend", "sqlite")
    if backend != "sqlite":
        raise ValueError("application_database_config.backend 目前只能是 sqlite")
    auto_create_parent = config.get("auto_create_parent", True)
    if not isinstance(auto_create_parent, bool):
        raise TypeError("application_database_config.auto_create_parent 必须是布尔值")
    if not auto_create_parent:
        raise ValueError("application_database_config.auto_create_parent 必须为 True")
    echo = config.get("echo", False)
    if not isinstance(echo, bool):
        raise TypeError("application_database_config.echo 必须是布尔值")
    timeout_seconds = config.get("timeout_seconds", 30.0)
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds,
        (int, float),
    ):
        raise TypeError("application_database_config.timeout_seconds 必须是数字")
    normalized_timeout = float(timeout_seconds)
    if normalized_timeout <= 0 or normalized_timeout > 300:
        raise ValueError("application_database_config.timeout_seconds 必须位于 0 到 300 之间")

    dependent_paths = [
        value
        for value in (
            memory.get("database_path") if memory is not None else None,
            (context_compact.get("database_path") if context_compact is not None else None),
        )
        if value is not None
    ]
    enabled = configured_enabled or bool(dependent_paths)
    if not enabled:
        return create_disabled_application_database_state()

    raw_database_path = config.get(
        "database_path",
        dependent_paths[0]
        if dependent_paths
        else os.environ.get(
            APPLICATION_DATABASE_PATH_ENV,
            str(DEFAULT_APPLICATION_DATABASE_PATH),
        ),
    )
    if not isinstance(raw_database_path, (str, Path)):
        raise TypeError("application_database_config.database_path 必须是字符串或 Path")
    database_path = validate_application_database_path(
        raw_database_path,
        input_root=request["root_directory"],
        checkpoint_path=checkpoint_path,
    )
    for dependent_path in dependent_paths:
        if Path(dependent_path).expanduser().resolve() != database_path:
            raise ValueError("应用数据库、Memory 与 Context Summary 必须共用同一个 SQLite 文件")
    return ApplicationDatabaseState(
        enabled=True,
        backend="sqlite",
        database_path=str(database_path),
        checkpoint_path=(
            str(Path(checkpoint_path).expanduser().resolve())
            if checkpoint_path is not None
            else None
        ),
        auto_create_parent=True,
        echo=echo,
        timeout_seconds=normalized_timeout,
        status="pending",
        last_error=None,
    )


def create_initial_state(
    request: RequestState,
    workspace: WorkspaceState,
    *,
    prompt_config: Mapping[str, object] | None = None,
    hook_config: Mapping[str, object] | None = None,
    llm_config: Mapping[str, object] | None = None,
    skill_registry_path: str | Path | None = None,
    memory_config: Mapping[str, object] | None = None,
    context_compact_config: Mapping[str, object] | None = None,
    application_database_config: Mapping[str, object] | None = None,
    checkpoint_path: str | Path | None = None,
    thread_id: str | None = None,
) -> FileGovernanceState:
    """创建可直接传给顶层 LangGraph 的完整初始状态。

    Args:
        request: 用户指定的扫描目录、扩展名、判断阈值和可选证据路径。
        workspace: 只读输入根目录以及可写产物、报告目录。
        prompt_config: 可选 System Prompt 配置；省略时保持完全关闭。
        hook_config: 可选生命周期 Hook 配置；省略时保持完全关闭。
        llm_config: 可选单模型或多 Profile LLM 配置；省略时关闭真实模型并使用
            安全 Mock Profile，旧版单模型配置会自动转换为默认 Profile。
        skill_registry_path: 可选受控 Skill 注册表路径；省略时使用项目默认资源。
        memory_config: 可选短期与长期 Memory 配置；省略时不访问应用数据库。
        context_compact_config: 可选 Context Compact 阈值、预览与持久化配置。
        application_database_config: 可选运行历史、工具审计与人工选择数据库配置。
        checkpoint_path: 可选 SQLite Checkpointer 路径，用于与应用数据库隔离。
        thread_id: 可选 Checkpointer 线程 ID；非 CLI 调用可在初始化时回退为 run_id。

    Returns:
        所有 reducer 列表、模型 Profile 路由、生命周期配置、证据和人工审核字段
        均已初始化的状态。
    """
    normalized_request = dict(request)
    normalized_request.setdefault("pdf_match_threshold", 0.82)
    normalized_request.setdefault("delivery_log_path", None)
    if thread_id is not None and (not isinstance(thread_id, str) or not thread_id.strip()):
        raise ValueError("thread_id 必须是非空字符串或 None")
    memory = create_memory_state(
        normalized_request,
        memory_config,
        checkpoint_path=checkpoint_path,
    )
    context_compact = create_context_compact_state(
        normalized_request,
        context_compact_config,
        checkpoint_path=checkpoint_path,
    )
    application_database = create_application_database_state(
        normalized_request,
        application_database_config,
        memory=memory,
        context_compact=context_compact,
        checkpoint_path=checkpoint_path,
    )
    return FileGovernanceState(
        run={
            "run_id": "",
            "thread_id": thread_id.strip() if thread_id is not None else "",
            "status": "created",
            "current_stage": "created",
            "started_at": None,
            "finished_at": None,
        },
        request=normalized_request,
        workspace=dict(workspace),
        prompt=create_prompt_state(prompt_config),
        hooks=create_hook_config_state(hook_config),
        llm=create_llm_config_state(llm_config),
        team=create_team_state(),
        skill_registry=create_pending_skill_registry(skill_registry_path),
        memory=memory,
        context_compact=context_compact,
        application_database=application_database,
        hook_events=[],
        todos=[],
        tasks=[],
        team_messages=[],
        llm_calls=[],
        human_review={
            "pending_group_ids": [],
            "selections": {},
            "review_note": None,
        },
        report={
            "summary": "",
            "report_markdown": "",
            "warnings": [],
            "report_path": None,
            "generated_at": None,
        },
        files=[],
        documents=[],
        version_groups=[],
        diffs=[],
        version_edges=[],
        branches=[],
        version_chains=[],
        pdf_exports=[],
        deliveries=[],
        decisions=[],
        errors=[],
    )
