from __future__ import annotations

import sysconfig
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from app.state.models import (
    FileGovernanceState,
    HookConfigState,
    PromptState,
    RequestState,
    WorkspaceState,
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
            raise ValueError(
                f"Hook {raw_name} 的失败策略只能是 block 或 ignore"
            )
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


def create_initial_state(
    request: RequestState,
    workspace: WorkspaceState,
    *,
    prompt_config: Mapping[str, object] | None = None,
    hook_config: Mapping[str, object] | None = None,
) -> FileGovernanceState:
    """创建可直接传给顶层 LangGraph 的完整初始状态。

    Args:
        request: 用户指定的扫描目录、扩展名、判断阈值和可选证据路径。
        workspace: 只读输入根目录以及可写产物、报告目录。
        prompt_config: 可选 System Prompt 配置；省略时保持完全关闭。
        hook_config: 可选生命周期 Hook 配置；省略时保持完全关闭。

    Returns:
        所有 reducer 列表、生命周期配置、证据和人工审核字段均已初始化的状态。
    """
    normalized_request = dict(request)
    normalized_request.setdefault("pdf_match_threshold", 0.82)
    normalized_request.setdefault("delivery_log_path", None)
    return FileGovernanceState(
        run={
            "run_id": "",
            "status": "created",
            "current_stage": "created",
            "started_at": None,
            "finished_at": None,
        },
        request=normalized_request,
        workspace=dict(workspace),
        prompt=create_prompt_state(prompt_config),
        hooks=create_hook_config_state(hook_config),
        hook_events=[],
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
