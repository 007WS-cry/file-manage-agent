from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token

"""本模块使用 ContextVar 在并发安全边界内传递脱敏的运行日志关联字段。"""


# JSON 日志允许跨调用自动传播的固定脱敏上下文字段。
LOG_CONTEXT_KEYS = frozenset(
    {
        "service",
        "run_id",
        "job_id",
        "worker_id",
        "schedule_id",
        "task_id",
    }
)

# 当前异步任务或线程使用的不可变日志上下文副本。
_LOG_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar(
    "file_governance_log_context",
    default=None,
)


def _normalize_log_context(values: Mapping[str, object]) -> dict[str, str]:
    """校验并复制允许进入统一日志的脱敏关联字段。

    Args:
        values: 等待绑定的字段和值。

    Returns:
        只包含白名单字段和有限非空字符串值的上下文。

    Raises:
        ValueError: 出现未知字段、空值或超长值时抛出。
    """
    unknown_fields = set(values) - LOG_CONTEXT_KEYS
    if unknown_fields:
        raise ValueError(f"日志上下文包含未知字段：{sorted(unknown_fields)}")
    normalized: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text or len(text) > 256:
            raise ValueError(f"日志上下文 {key} 必须是长度不超过 256 的非空值")
        normalized[key] = text
    return normalized


def get_log_context() -> dict[str, str]:
    """读取当前并发边界中的日志上下文副本。

    Returns:
        修改后不会影响 ContextVar 原值的脱敏字段字典。
    """
    return dict(_LOG_CONTEXT.get() or {})


@contextmanager
def bind_log_context(**values: object) -> Iterator[None]:
    """在当前调用边界临时合并统一日志关联字段。

    Args:
        **values: ``service``、运行、任务、Worker 或计划等白名单字段。

    Yields:
        新上下文生效期间的调用控制权。
    """
    merged = get_log_context()
    merged.update(_normalize_log_context(values))
    token: Token[dict[str, str] | None] = _LOG_CONTEXT.set(merged)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)
