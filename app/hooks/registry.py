from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from app.hooks import HookFunction
from app.hooks.builtin import (
    cleanup_run_resources_hook,
    enrich_run_state_hook,
    flush_tool_audit_hook,
    initialize_tool_audit_hook,
    validate_report_result_hook,
    validate_request_envelope_hook,
)

"""本模块维护静态 Hook 白名单，并拒绝配置驱动的动态导入或任意代码执行。"""

# 当前版本允许按名称调用的内置 Hook，不接受模块路径、表达式或运行时注册字符串。
DEFAULT_HOOK_REGISTRY: Mapping[str, HookFunction] = MappingProxyType(
    {
        "validate_request_envelope_hook": validate_request_envelope_hook,
        "enrich_run_state_hook": enrich_run_state_hook,
        "initialize_tool_audit_hook": initialize_tool_audit_hook,
        "validate_report_result_hook": validate_report_result_hook,
        "flush_tool_audit_hook": flush_tool_audit_hook,
        "cleanup_run_resources_hook": cleanup_run_resources_hook,
    }
)


def get_hook_registry() -> dict[str, HookFunction]:
    """返回默认静态 Hook 注册表的浅拷贝。

    Returns:
        Hook 名称到函数的独立字典；修改该字典不会改变默认白名单。
    """
    return dict(DEFAULT_HOOK_REGISTRY)


def validate_hook_registrations(
    hook_names: Iterable[str],
    *,
    registry: Mapping[str, HookFunction] | None = None,
) -> None:
    """验证执行计划中的 Hook 名称均唯一且存在于静态注册表。

    函数只执行字典查找，不解析点号导入路径、不调用 ``eval``，也不会扫描插件或
    文件系统。测试可以显式传入隔离注册表，生产调用默认使用固定内置白名单。

    Args:
        hook_names: 按执行顺序排列的 Hook 名称。
        registry: 可选隔离注册表；省略时使用默认静态白名单。

    Raises:
        TypeError: Hook 名称不是字符串或注册值不可调用时抛出。
        ValueError: Hook 名称为空、重复或未注册时抛出。
    """
    selected_registry = registry if registry is not None else DEFAULT_HOOK_REGISTRY
    seen: set[str] = set()
    for hook_name in hook_names:
        if not isinstance(hook_name, str):
            raise TypeError("Hook 名称必须是字符串")
        normalized_name = hook_name.strip()
        if not normalized_name:
            raise ValueError("Hook 名称不得为空")
        if normalized_name in seen:
            raise ValueError(f"Hook 执行计划包含重复名称：{normalized_name}")
        seen.add(normalized_name)
        hook_function = selected_registry.get(normalized_name)
        if hook_function is None:
            raise ValueError(f"Hook 未在静态注册表中登记：{normalized_name}")
        if not callable(hook_function):
            raise TypeError(f"Hook 注册值不可调用：{normalized_name}")


def resolve_registered_hook(
    hook_name: str,
    *,
    registry: Mapping[str, HookFunction] | None = None,
) -> HookFunction:
    """从静态白名单解析单个 Hook 函数。

    Args:
        hook_name: 配置中声明的 Hook 名称。
        registry: 可选隔离注册表；省略时使用默认静态白名单。

    Returns:
        已通过唯一名称验证的 Hook 函数。

    Raises:
        TypeError: 名称类型或注册值不正确时抛出。
        ValueError: 名称为空或未注册时抛出。
    """
    validate_hook_registrations([hook_name], registry=registry)
    selected_registry = registry if registry is not None else DEFAULT_HOOK_REGISTRY
    return selected_registry[hook_name.strip()]
