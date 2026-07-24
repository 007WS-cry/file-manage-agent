from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from app.state.models import (
    ErrorRecord,
    RecoveryCategoryPolicyState,
    RecoveryPolicyState,
)
from app.utils.runtime import utc_now_iso

"""本模块负责校验、规范化和查询 0.6.1 的确定性错误恢复策略，不执行图节点或 I/O。"""


# 0.6.1 状态协议允许分类的全部错误类别，顺序用于生成稳定策略快照。
RECOVERY_ERROR_CATEGORIES = (
    "filesystem",
    "parse",
    "comparison",
    "evidence",
    "llm",
    "validation",
    "protocol",
    "prompt",
    "hook",
    "memory",
    "skill",
    "context",
    "database",
    "checkpoint",
    "timeout",
    "unknown",
)

# 恢复状态允许登记的安全降级动作，不包含任何删除、覆盖或外部写入操作。
RECOVERY_FALLBACK_ACTIONS = (
    "skip_file",
    "coordinator",
    "no_memory",
    "default_skill",
    "keep_context",
    "partial_result",
)

# 未知或未单独配置错误类别使用的保守默认策略。
DEFAULT_RECOVERY_POLICY = {
    "retryable": False,
    "max_retries": 0,
    "initial_backoff_seconds": 0.5,
    "backoff_multiplier": 2.0,
    "max_backoff_seconds": 8.0,
    "fallback": None,
    "requires_human": True,
}

# 各错误类别在默认策略之上的最小覆盖，保持策略确定且便于测试。
DEFAULT_RECOVERY_CATEGORY_OVERRIDES = {
    "filesystem": {
        "retryable": True,
        "max_retries": 1,
    },
    "parse": {
        "fallback": "skip_file",
        "requires_human": False,
    },
    "comparison": {
        "fallback": "partial_result",
        "requires_human": True,
    },
    "evidence": {
        "retryable": True,
        "max_retries": 1,
        "fallback": "partial_result",
        "requires_human": False,
    },
    "llm": {
        "retryable": True,
        "max_retries": 1,
        "fallback": "coordinator",
        "requires_human": False,
    },
    "protocol": {
        "fallback": "coordinator",
        "requires_human": True,
    },
    "memory": {
        "retryable": True,
        "max_retries": 1,
        "fallback": "no_memory",
        "requires_human": False,
    },
    "skill": {
        "retryable": True,
        "max_retries": 1,
        "fallback": "default_skill",
        "requires_human": False,
    },
    "context": {
        "fallback": "keep_context",
        "requires_human": False,
    },
    "database": {
        "retryable": True,
        "max_retries": 2,
        "initial_backoff_seconds": 1.0,
        "max_backoff_seconds": 10.0,
    },
    "checkpoint": {
        "retryable": True,
        "max_retries": 1,
        "initial_backoff_seconds": 1.0,
    },
    "timeout": {
        "retryable": True,
        "max_retries": 2,
        "initial_backoff_seconds": 1.0,
        "max_backoff_seconds": 10.0,
    },
}


def _reject_unknown_fields(
    value: Mapping[str, object],
    *,
    allowed_fields: set[str],
    field_name: str,
) -> None:
    """拒绝恢复配置中的未知字段，避免拼写错误被静默忽略。

    Args:
        value: 等待检查的恢复配置映射。
        allowed_fields: 当前层级允许出现的字段名称。
        field_name: 用于错误信息的配置层级名称。

    Raises:
        ValueError: 配置包含协议未定义的字段时抛出。
    """
    unknown_fields = sorted(set(value) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"{field_name} 包含未知字段：{', '.join(unknown_fields)}")


def _normalize_nonnegative_integer(value: object, *, field_name: str) -> int:
    """校验恢复配置中的非负整数。

    Args:
        value: 等待校验的配置值。
        field_name: 用于错误信息的完整字段名称。

    Returns:
        位于零到十之间的整数。

    Raises:
        TypeError: 配置值不是整数或错误地使用布尔值时抛出。
        ValueError: 配置值超出允许范围时抛出。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} 必须是整数")
    if value < 0 or value > 10:
        raise ValueError(f"{field_name} 必须位于 0 到 10 之间")
    return value


def _normalize_number(
    value: object,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
) -> float:
    """校验恢复配置中的有限数值范围。

    Args:
        value: 等待校验的配置值。
        field_name: 用于错误信息的完整字段名称。
        minimum: 允许的最小值。
        maximum: 允许的最大值。

    Returns:
        转换为浮点数后的合法配置值。

    Raises:
        TypeError: 配置值不是整数或浮点数时抛出。
        ValueError: 配置值超出允许范围时抛出。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} 必须是数字")
    normalized = float(value)
    if normalized < minimum or normalized > maximum:
        raise ValueError(f"{field_name} 必须位于 {minimum:g} 到 {maximum:g} 之间")
    return normalized


def _normalize_policy_rule(
    value: Mapping[str, object],
    *,
    base: Mapping[str, object],
    field_name: str,
) -> RecoveryCategoryPolicyState:
    """将一个默认策略和类别覆盖合并为完整、可序列化的恢复规则。

    Args:
        value: 当前类别显式提供的策略字段。
        base: 当前规则继承的完整或部分基础策略。
        field_name: 用于错误信息的配置层级名称。

    Returns:
        所有字段均已补齐并完成范围校验的恢复规则。

    Raises:
        TypeError: 布尔值、数值或降级动作类型错误时抛出。
        ValueError: 字段未知、范围越界或重试语义矛盾时抛出。
    """
    allowed_fields = {
        "retryable",
        "max_retries",
        "initial_backoff_seconds",
        "backoff_multiplier",
        "max_backoff_seconds",
        "fallback",
        "requires_human",
    }
    _reject_unknown_fields(
        value,
        allowed_fields=allowed_fields,
        field_name=field_name,
    )
    merged = {**base, **value}

    retryable = merged.get("retryable", False)
    if not isinstance(retryable, bool):
        raise TypeError(f"{field_name}.retryable 必须是布尔值")
    max_retries = _normalize_nonnegative_integer(
        merged.get("max_retries", 0),
        field_name=f"{field_name}.max_retries",
    )
    if retryable and max_retries == 0:
        raise ValueError(f"{field_name} 允许重试时 max_retries 必须大于零")
    if not retryable and max_retries != 0:
        raise ValueError(f"{field_name} 禁止重试时 max_retries 必须为零")

    initial_backoff_seconds = _normalize_number(
        merged.get("initial_backoff_seconds", 0.5),
        field_name=f"{field_name}.initial_backoff_seconds",
        minimum=0.0,
        maximum=300.0,
    )
    backoff_multiplier = _normalize_number(
        merged.get("backoff_multiplier", 2.0),
        field_name=f"{field_name}.backoff_multiplier",
        minimum=1.0,
        maximum=10.0,
    )
    max_backoff_seconds = _normalize_number(
        merged.get("max_backoff_seconds", 8.0),
        field_name=f"{field_name}.max_backoff_seconds",
        minimum=0.0,
        maximum=3600.0,
    )
    if max_backoff_seconds < initial_backoff_seconds:
        raise ValueError(f"{field_name}.max_backoff_seconds 不得小于 initial_backoff_seconds")

    fallback = merged.get("fallback")
    if fallback is not None and not isinstance(fallback, str):
        raise TypeError(f"{field_name}.fallback 必须是字符串或 null")
    if fallback not in {None, *RECOVERY_FALLBACK_ACTIONS}:
        raise ValueError(f"{field_name}.fallback 不是允许的安全降级动作")

    requires_human = merged.get("requires_human", True)
    if not isinstance(requires_human, bool):
        raise TypeError(f"{field_name}.requires_human 必须是布尔值")

    return RecoveryCategoryPolicyState(
        retryable=retryable,
        max_retries=max_retries,
        initial_backoff_seconds=initial_backoff_seconds,
        backoff_multiplier=backoff_multiplier,
        max_backoff_seconds=max_backoff_seconds,
        fallback=cast(
            Literal[
                "skip_file",
                "coordinator",
                "no_memory",
                "default_skill",
                "keep_context",
                "partial_result",
            ]
            | None,
            fallback,
        ),
        requires_human=requires_human,
    )


def create_recovery_policy_state(
    recovery_config: Mapping[str, object] | None = None,
) -> RecoveryPolicyState:
    """根据可选配置创建完整、确定且不执行副作用的恢复策略快照。

    Args:
        recovery_config: 可选恢复开关、默认策略和错误类别覆盖配置。

    Returns:
        包含全部错误类别规则的独立恢复策略状态。

    Raises:
        TypeError: 配置对象、类别名或规则字段类型不正确时抛出。
        ValueError: 配置包含未知字段、未知类别、越界值或重试语义矛盾时抛出。
    """
    if recovery_config is not None and not isinstance(recovery_config, Mapping):
        raise TypeError("recovery_config 必须是映射或 None")
    config = dict(recovery_config or {})
    _reject_unknown_fields(
        config,
        allowed_fields={
            "enabled",
            "default_policy",
            "categories",
            "category_policies",
        },
        field_name="recovery_config",
    )
    if "categories" in config and "category_policies" in config:
        raise ValueError("recovery_config 不能同时包含 categories 和 category_policies")

    enabled = config.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TypeError("recovery_config.enabled 必须是布尔值")

    raw_default_policy = config.get("default_policy", {})
    if not isinstance(raw_default_policy, Mapping):
        raise TypeError("recovery_config.default_policy 必须是映射")
    default_policy = _normalize_policy_rule(
        raw_default_policy,
        base=DEFAULT_RECOVERY_POLICY,
        field_name="recovery_config.default_policy",
    )

    raw_categories = config.get(
        "categories",
        config.get("category_policies", {}),
    )
    if not isinstance(raw_categories, Mapping):
        raise TypeError("recovery_config.categories 必须是映射")
    for raw_category, raw_rule in raw_categories.items():
        if not isinstance(raw_category, str):
            raise TypeError("recovery_config.categories 的键必须是字符串")
        if raw_category not in RECOVERY_ERROR_CATEGORIES:
            raise ValueError(f"recovery_config.categories 包含未知错误类别：{raw_category}")
        if not isinstance(raw_rule, Mapping):
            raise TypeError(f"recovery_config.categories.{raw_category} 必须是映射")

    category_policies: dict[str, RecoveryCategoryPolicyState] = {}
    for category in RECOVERY_ERROR_CATEGORIES:
        built_in_override = DEFAULT_RECOVERY_CATEGORY_OVERRIDES.get(category, {})
        configured_override = raw_categories.get(category, {})
        category_base = _normalize_policy_rule(
            built_in_override,
            base=default_policy,
            field_name=f"default_recovery_categories.{category}",
        )
        category_policies[category] = _normalize_policy_rule(
            cast(Mapping[str, object], configured_override),
            base=category_base,
            field_name=f"recovery_config.categories.{category}",
        )

    return RecoveryPolicyState(
        enabled=enabled,
        default_policy=dict(default_policy),
        category_policies={
            category: dict(policy) for category, policy in category_policies.items()
        },
    )


def copy_recovery_policy_state(
    policy: Mapping[str, object] | None,
) -> RecoveryPolicyState:
    """复制恢复策略并为旧 checkpoint 或缺失字段补齐安全默认值。

    Args:
        policy: 当前状态中的可选恢复策略映射。

    Returns:
        与输入解除可变引用关系的完整恢复策略状态。
    """
    if policy is None:
        return create_recovery_policy_state()
    return create_recovery_policy_state(
        {
            "enabled": policy.get("enabled", True),
            "default_policy": policy.get("default_policy", {}),
            "category_policies": policy.get(
                "category_policies",
                policy.get("categories", {}),
            ),
        }
    )


def resolve_category_policy(
    policy: Mapping[str, object],
    category: str,
) -> RecoveryCategoryPolicyState:
    """读取一个错误类别的完整策略，未知类别保守回退到默认策略。

    Args:
        policy: 已规范化或来自 checkpoint 的恢复策略映射。
        category: 等待查询的错误类别名称。

    Returns:
        与策略状态解除可变引用关系的完整类别规则。

    Raises:
        TypeError: 类别名称不是字符串时抛出。
    """
    if not isinstance(category, str):
        raise TypeError("category 必须是字符串")
    normalized_policy = copy_recovery_policy_state(policy)
    selected = normalized_policy["category_policies"].get(
        category,
        normalized_policy["default_policy"],
    )
    return RecoveryCategoryPolicyState(**dict(selected))


def calculate_retry_backoff(
    policy: Mapping[str, object],
    retry_number: int,
) -> float:
    """根据完整类别策略计算第几次额外重试前的确定性退避时间。

    Args:
        policy: 已规范化的单个错误类别策略。
        retry_number: 从一开始计数的额外重试序号。

    Returns:
        应等待的秒数，不超过策略声明的最大退避值。

    Raises:
        TypeError: 重试序号不是整数或策略字段类型错误时抛出。
        ValueError: 重试序号不为正数或超过策略允许次数时抛出。
    """
    normalized_policy = _normalize_policy_rule(
        policy,
        base=DEFAULT_RECOVERY_POLICY,
        field_name="policy",
    )
    if isinstance(retry_number, bool) or not isinstance(retry_number, int):
        raise TypeError("retry_number 必须是整数")
    if retry_number < 1:
        raise ValueError("retry_number 必须大于零")
    if retry_number > normalized_policy["max_retries"]:
        raise ValueError("retry_number 超过策略允许的最大重试次数")
    delay = normalized_policy["initial_backoff_seconds"] * (
        normalized_policy["backoff_multiplier"] ** (retry_number - 1)
    )
    return min(delay, normalized_policy["max_backoff_seconds"])


def recommend_recovery_action(
    error: Mapping[str, Any],
    policy: Mapping[str, object],
) -> Literal["none", "retry", "fallback", "wait_human", "abort"]:
    """根据错误生命周期和策略快照推荐下一步恢复动作。

    本函数只返回确定性动作，不修改状态、不等待、不访问数据库，也不调用图节点。

    Args:
        error: 已捕获的结构化错误记录。
        policy: 当前运行使用的恢复策略快照。

    Returns:
        无需处理、重试、安全降级、等待人工或终止中的一个动作。
    """
    normalized_policy = copy_recovery_policy_state(policy)
    if not normalized_policy["enabled"]:
        return "none"
    if error.get("status") in {"recovered", "fallback_applied"}:
        return "none"

    category_policy = resolve_category_policy(
        normalized_policy,
        str(error.get("category", "unknown")),
    )
    raw_retry_count = error.get("retry_count", 0)
    retry_count = (
        raw_retry_count
        if isinstance(raw_retry_count, int) and not isinstance(raw_retry_count, bool)
        else 0
    )
    if category_policy["retryable"] and retry_count < category_policy["max_retries"]:
        return "retry"
    if category_policy["fallback"] is not None:
        return "fallback"
    if category_policy["requires_human"]:
        return "wait_human"
    return "abort"


def apply_recovery_policy_to_error(
    error: Mapping[str, Any],
    policy: Mapping[str, object],
) -> ErrorRecord:
    """把错误类别策略复制到 ErrorRecord，并保留原错误事实和兼容字段。

    Args:
        error: 等待补齐恢复字段的错误记录或旧版错误映射。
        policy: 当前运行使用的恢复策略快照。

    Returns:
        具有完整重试、降级、人工恢复和生命周期字段的新 ErrorRecord。
    """
    raw_category = str(error.get("category", "unknown"))
    category = raw_category if raw_category in RECOVERY_ERROR_CATEGORIES else "unknown"
    category_policy = resolve_category_policy(policy, category)
    raw_status = error.get("status")
    status = (
        raw_status
        if raw_status
        in {
            "pending",
            "retrying",
            "fallback_applied",
            "waiting_human",
            "recovered",
            "failed",
        }
        else "pending"
    )
    raw_retry_count = error.get("retry_count", 0)
    retry_count = (
        raw_retry_count
        if isinstance(raw_retry_count, int) and not isinstance(raw_retry_count, bool)
        else 0
    )
    return ErrorRecord(
        id=str(error["id"]),
        stage=str(error["stage"]),
        node_name=str(error["node_name"]),
        category=cast(Any, category),
        exception_type=(
            str(error["exception_type"]) if error.get("exception_type") is not None else None
        ),
        message=str(error.get("message", "未提供错误说明")),
        related_file_id=(
            str(error["related_file_id"]) if error.get("related_file_id") is not None else None
        ),
        task_id=(str(error["task_id"]) if error.get("task_id") is not None else None),
        node_execution_id=(
            str(error["node_execution_id"]) if error.get("node_execution_id") is not None else None
        ),
        retryable=category_policy["retryable"],
        retry_count=retry_count,
        max_retries=category_policy["max_retries"],
        fallback=category_policy["fallback"],
        requires_human=category_policy["requires_human"],
        status=cast(Any, status),
        fatal=bool(error.get("fatal", False)),
        created_at=str(error.get("created_at") or utc_now_iso()),
        recovered_at=(
            str(error["recovered_at"]) if error.get("recovered_at") is not None else None
        ),
    )
