from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from app.hooks import HookFunction, HookPhase, HookResult
from app.hooks.registry import DEFAULT_HOOK_REGISTRY, resolve_registered_hook
from app.state.models import ErrorRecord, FileGovernanceState, HookEvent
from app.utils.error_context import create_node_error
from app.utils.runtime import utc_now_iso

"""本模块顺序执行静态 Hook，并保护固定 Agent 状态、聚合事件和阻断错误。"""

# Hook 允许修改的顶层字段；业务事实、请求、工作空间和原始文件列表均不可修改。
HOOK_MUTABLE_STATE_FIELDS = frozenset({"run", "report"})

# 固定 Agent 协议、团队与模型审计字段；生命周期 Hook 必须始终保持只读。
HOOK_AGENT_PROTECTED_STATE_FIELDS = frozenset(
    {"llm", "team", "team_messages", "llm_calls", "tasks", "todos"}
)


def are_hooks_enabled(state: FileGovernanceState) -> bool:
    """判断当前顶层状态是否启用了生命周期 Hooks。

    Args:
        state: 包含 Hook 配置的顶层治理状态。

    Returns:
        仅当 ``hooks.enabled`` 明确为 True 时返回 True。
    """
    hooks = state.get("hooks", {})
    return isinstance(hooks, Mapping) and hooks.get("enabled") is True


def resolve_hook_plan(
    state: FileGovernanceState,
    phase: HookPhase,
) -> list[str]:
    """复制当前生命周期阶段配置的 Hook 执行顺序。

    Args:
        state: 包含 Hook 配置的顶层治理状态。
        phase: before/after run/model 生命周期阶段。

    Returns:
        与状态对象解除可变引用关系的 Hook 名称列表。

    Raises:
        ValueError: 顶层 Hook 配置缺失或阶段值不是字符串列表时抛出。
    """
    hooks = state.get("hooks")
    if not isinstance(hooks, Mapping):
        raise ValueError("顶层状态缺少有效的 hooks 配置")
    raw_plan = hooks.get(phase)
    if not isinstance(raw_plan, list):
        raise ValueError(f"hooks.{phase} 必须是字符串列表")
    if any(not isinstance(item, str) or not item.strip() for item in raw_plan):
        raise ValueError(f"hooks.{phase} 只能包含非空 Hook 名称")
    return [item.strip() for item in raw_plan]


def _resolve_failure_policy(
    state: FileGovernanceState,
    hook_name: str,
) -> Literal["block", "ignore"]:
    """解析单个 Hook 实际使用的失败策略。

    Args:
        state: 包含默认策略和单 Hook 覆盖策略的顶层状态。
        hook_name: 等待解析策略的 Hook 名称。

    Returns:
        ``block`` 或 ``ignore`` 之一。

    Raises:
        ValueError: Hook 配置或策略值不符合状态协议时抛出。
    """
    hooks = state.get("hooks")
    if not isinstance(hooks, Mapping):
        raise ValueError("顶层状态缺少有效的 hooks 配置")
    raw_policies = hooks.get("failure_policies", {})
    if not isinstance(raw_policies, Mapping):
        raise ValueError("hooks.failure_policies 必须是对象")
    policy = raw_policies.get(hook_name, hooks.get("default_failure_policy", "block"))
    if policy not in {"block", "ignore"}:
        raise ValueError(f"Hook {hook_name} 的失败策略只能是 block 或 ignore")
    return cast(Literal["block", "ignore"], policy)


def _create_hook_event_id(
    state: FileGovernanceState,
    *,
    phase: HookPhase,
    sequence: int,
    hook_name: str,
) -> str:
    """根据运行、阶段和顺序生成可重复合并的 Hook 事件 ID。

    Args:
        state: 当前顶层治理状态。
        phase: Hook 生命周期阶段。
        sequence: Hook 在当前阶段的一基执行序号。
        hook_name: Hook 静态注册名称。

    Returns:
        64 个小写十六进制字符组成的稳定事件 ID。
    """
    run = state.get("run", {})
    run_id = run.get("run_id", "") if isinstance(run, Mapping) else ""
    identity = "\x1f".join((str(run_id), phase, str(sequence), hook_name))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _normalize_event_message(message: object) -> str:
    """生成适合 HookEvent 的简短单行说明。

    Args:
        message: Hook 返回或异常产生的原始说明。

    Returns:
        去除多余空白且最多 500 个字符的消息。
    """
    normalized = " ".join(str(message).split()) or "未提供 Hook 执行说明"
    return normalized[:500]


def record_hook_success(
    state: FileGovernanceState,
    *,
    phase: HookPhase,
    sequence: int,
    hook_name: str,
    message: str,
) -> HookEvent:
    """创建单个 Hook 执行成功事件。

    Args:
        state: 当前顶层治理状态。
        phase: Hook 生命周期阶段。
        sequence: Hook 在当前阶段的一基执行序号。
        hook_name: Hook 静态注册名称。
        message: Hook 返回的简短执行说明。

    Returns:
        可由 ``merge_by_id`` 合并的成功 HookEvent。
    """
    return HookEvent(
        id=_create_hook_event_id(
            state,
            phase=phase,
            sequence=sequence,
            hook_name=hook_name,
        ),
        phase=phase,
        sequence=sequence,
        hook_name=hook_name,
        status="success",
        failure_policy=_resolve_failure_policy(state, hook_name),
        message=_normalize_event_message(message),
        created_at=utc_now_iso(),
    )


def record_hook_failure(
    state: FileGovernanceState,
    *,
    phase: HookPhase,
    sequence: int,
    hook_name: str,
    error: Exception,
) -> HookEvent:
    """创建单个 Hook 执行失败事件并记录实际失败策略。

    Args:
        state: 当前顶层治理状态。
        phase: Hook 生命周期阶段。
        sequence: Hook 在当前阶段的一基执行序号。
        hook_name: Hook 静态注册名称。
        error: Hook 执行或结果校验产生的异常。

    Returns:
        可由 ``merge_by_id`` 合并的失败 HookEvent。
    """
    return HookEvent(
        id=_create_hook_event_id(
            state,
            phase=phase,
            sequence=sequence,
            hook_name=hook_name,
        ),
        phase=phase,
        sequence=sequence,
        hook_name=hook_name,
        status="failed",
        failure_policy=_resolve_failure_policy(state, hook_name),
        message=_normalize_event_message(error),
        created_at=utc_now_iso(),
    )


def record_skipped_hooks(
    state: FileGovernanceState,
    *,
    phase: HookPhase,
    hook_names: Sequence[str],
) -> list[HookEvent]:
    """为配置关闭而跳过的 Hook 按原顺序创建事件。

    Args:
        state: 当前顶层治理状态。
        phase: Hook 生命周期阶段。
        hook_names: 当前阶段配置的 Hook 名称序列。

    Returns:
        状态均为 ``skipped`` 且序号从一开始的 HookEvent 列表。
    """
    events: list[HookEvent] = []
    for sequence, hook_name in enumerate(hook_names, start=1):
        events.append(
            HookEvent(
                id=_create_hook_event_id(
                    state,
                    phase=phase,
                    sequence=sequence,
                    hook_name=hook_name,
                ),
                phase=phase,
                sequence=sequence,
                hook_name=hook_name,
                status="skipped",
                failure_policy=_resolve_failure_policy(state, hook_name),
                message="生命周期 Hooks 已关闭，本 Hook 未执行。",
                created_at=utc_now_iso(),
            )
        )
    return events


def has_next_hook(hook_names: Sequence[str], next_index: int) -> bool:
    """判断执行计划是否仍有尚未处理的 Hook。

    Args:
        hook_names: 当前阶段的 Hook 执行计划。
        next_index: 下一项的零基索引。

    Returns:
        索引位于计划范围内时返回 True。

    Raises:
        ValueError: 索引小于零时抛出。
    """
    if next_index < 0:
        raise ValueError("next_index 不得小于零")
    return next_index < len(hook_names)


def invoke_registered_hook(
    hook_name: str,
    state: FileGovernanceState,
    *,
    registry: Mapping[str, HookFunction] | None = None,
) -> HookResult:
    """从静态注册表解析并调用单个 Hook，再验证返回协议。

    Args:
        hook_name: 等待调用的 Hook 静态名称。
        state: 当前 Hook 可读取的顶层治理状态。
        registry: 可选隔离注册表；省略时使用默认内置白名单。

    Returns:
        包含简短说明和受限状态更新的 HookResult。

    Raises:
        TypeError: Hook 返回值、说明或状态更新类型不正确时抛出。
        ValueError: Hook 未注册、尝试修改固定 Agent 状态或返回其他受保护字段时抛出。
    """
    selected_registry = registry if registry is not None else DEFAULT_HOOK_REGISTRY
    hook_function = resolve_registered_hook(hook_name, registry=selected_registry)
    raw_result = hook_function(state)
    if not isinstance(raw_result, Mapping):
        raise TypeError(f"Hook {hook_name} 必须返回 HookResult 对象")
    message = raw_result.get("message")
    state_update = raw_result.get("state_update")
    if not isinstance(message, str) or not message.strip():
        raise TypeError(f"Hook {hook_name} 必须返回非空 message")
    if not isinstance(state_update, Mapping):
        raise TypeError(f"Hook {hook_name} 的 state_update 必须是对象")

    normalized_update = dict(state_update)
    protected_agent_fields = sorted(
        set(normalized_update) & HOOK_AGENT_PROTECTED_STATE_FIELDS
    )
    if protected_agent_fields:
        raise ValueError(
            f"Hook {hook_name} 不得修改顶层字段（固定 Agent 状态）："
            f"{', '.join(protected_agent_fields)}"
        )
    disallowed_fields = sorted(set(normalized_update) - HOOK_MUTABLE_STATE_FIELDS)
    if disallowed_fields:
        raise ValueError(
            f"Hook {hook_name} 不得修改顶层字段：{', '.join(disallowed_fields)}"
        )
    for field_name, value in normalized_update.items():
        if not isinstance(value, Mapping):
            raise TypeError(f"Hook 更新字段 {field_name} 必须是完整对象")
        current_value = state.get(field_name)
        if not isinstance(current_value, Mapping):
            raise ValueError(f"顶层状态缺少可更新的 {field_name} 对象")
        if set(value) != set(current_value):
            raise ValueError(f"Hook 更新字段 {field_name} 必须返回完整对象")
        normalized_update[field_name] = dict(value)
    return HookResult(message=message.strip(), state_update=normalized_update)


def should_block_hook_failure(
    state: FileGovernanceState,
    hook_name: str,
) -> bool:
    """判断指定 Hook 失败后是否必须阻断运行。

    Args:
        state: 包含 Hook 失败策略的顶层治理状态。
        hook_name: 执行失败的 Hook 名称。

    Returns:
        实际策略为 ``block`` 时返回 True，否则返回 False。
    """
    return _resolve_failure_policy(state, hook_name) == "block"


def mark_hook_result_blocked(
    state: FileGovernanceState,
    *,
    phase: HookPhase,
    hook_name: str,
    event: HookEvent,
) -> ErrorRecord:
    """把阻断型 Hook 失败转换为顶层致命 ErrorRecord。

    Args:
        state: 当前顶层治理状态，用于绑定运行、任务和节点执行标识。
        phase: Hook 生命周期阶段。
        hook_name: 执行失败的 Hook 名称。
        event: 已生成并完成消息收敛的失败事件。

    Returns:
        类别为 ``hook``、可由 reducer 合并的致命错误记录。
    """
    return create_node_error(
        state,
        stage=phase,
        node_name=hook_name,
        category="hook",
        message=f"生命周期 Hook 执行失败：{event['message']}",
        fatal=True,
    )


def aggregate_hook_result(
    *,
    state_updates: Mapping[str, Any],
    hook_events: Sequence[HookEvent],
    errors: Sequence[ErrorRecord],
) -> dict[str, Any]:
    """汇总本阶段的受限状态更新、新事件和阻断型错误。

    Args:
        state_updates: 成功 Hook 产生的最终顶层字段值。
        hook_events: 本阶段新产生的成功、失败或跳过事件。
        errors: 仅由阻断策略产生的致命错误。

    Returns:
        可作为 LangGraph 节点更新值的独立字典。
    """
    result = dict(state_updates)
    if hook_events:
        result["hook_events"] = [dict(event) for event in hook_events]
    if errors:
        result["errors"] = [dict(error) for error in errors]
    return result


def execute_hook_phase(
    state: FileGovernanceState,
    phase: HookPhase,
    *,
    registry: Mapping[str, HookFunction] | None = None,
) -> dict[str, Any]:
    """顺序执行一个生命周期阶段并聚合成功、忽略和阻断结果。

    每个 Hook 只从静态注册表解析。单个 Hook 失败不会阻止后续 Hook 执行，从而
    保证 after_run 阶段的审计收口和安全清理仍有机会执行；只有 ``block`` 策略会
    额外生成致命错误，``ignore`` 只保留失败事件。

    Args:
        state: 当前文件治理顶层状态。
        phase: 等待执行的生命周期阶段。
        registry: 可选隔离注册表；省略时使用默认内置白名单。

    Returns:
        包含受限状态更新、新 HookEvent 和阻断错误的 LangGraph 节点更新字典。

    Raises:
        ValueError: 顶层 Hook 配置或执行计划结构不合法时抛出。
    """
    hook_names = resolve_hook_plan(state, phase)
    if not are_hooks_enabled(state):
        return aggregate_hook_result(
            state_updates={},
            hook_events=record_skipped_hooks(
                state,
                phase=phase,
                hook_names=hook_names,
            ),
            errors=[],
        )

    selected_registry = registry if registry is not None else DEFAULT_HOOK_REGISTRY
    working_state = cast(FileGovernanceState, dict(state))
    state_updates: dict[str, Any] = {}
    events: list[HookEvent] = []
    errors: list[ErrorRecord] = []
    next_index = 0

    while has_next_hook(hook_names, next_index):
        hook_name = hook_names[next_index]
        sequence = next_index + 1
        try:
            hook_result = invoke_registered_hook(
                hook_name,
                working_state,
                registry=selected_registry,
            )
            for field_name, value in hook_result["state_update"].items():
                working_state[field_name] = value
                state_updates[field_name] = value
            event = record_hook_success(
                working_state,
                phase=phase,
                sequence=sequence,
                hook_name=hook_name,
                message=hook_result["message"],
            )
        except Exception as exc:
            event = record_hook_failure(
                working_state,
                phase=phase,
                sequence=sequence,
                hook_name=hook_name,
                error=exc,
            )
            if should_block_hook_failure(working_state, hook_name):
                errors.append(
                    mark_hook_result_blocked(
                        working_state,
                        phase=phase,
                        hook_name=hook_name,
                        event=event,
                    )
                )
        events.append(event)
        next_index += 1

    return aggregate_hook_result(
        state_updates=state_updates,
        hook_events=events,
        errors=errors,
    )


def execute_before_run_hooks(
    state: FileGovernanceState,
    *,
    registry: Mapping[str, HookFunction] | None = None,
) -> dict[str, Any]:
    """执行顶层业务流程开始前配置的 Hooks。

    Args:
        state: 初始化完成的顶层治理状态。
        registry: 可选隔离注册表，主要用于单元测试。

    Returns:
        ``before_run`` 阶段的状态更新、事件和阻断错误。
    """
    return execute_hook_phase(state, "before_run", registry=registry)


def execute_after_run_hooks(
    state: FileGovernanceState,
    *,
    registry: Mapping[str, HookFunction] | None = None,
) -> dict[str, Any]:
    """执行报告生成后、运行最终收口前配置的 Hooks。

    Args:
        state: 已生成报告的顶层治理状态。
        registry: 可选隔离注册表，主要用于单元测试。

    Returns:
        ``after_run`` 阶段的状态更新、事件和阻断错误。
    """
    return execute_hook_phase(state, "after_run", registry=registry)
