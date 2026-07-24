from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.state.models import ErrorRecord

"""本模块提供运行时间、结构化错误和本地路径关系判断等通用辅助函数。"""


# 结构化错误允许登记的安全降级动作，用于校验调用方传入的恢复元数据。
RECOVERY_FALLBACK_ACTIONS = frozenset(
    {
        "skip_file",
        "coordinator",
        "no_memory",
        "default_skill",
        "keep_context",
        "partial_result",
    }
)

# ErrorRecord 允许使用的错误生命周期状态，用于拒绝未知恢复状态。
ERROR_LIFECYCLE_STATUSES = frozenset(
    {
        "pending",
        "retrying",
        "fallback_applied",
        "waiting_human",
        "recovered",
        "failed",
    }
)


def utc_now_iso() -> str:
    """返回带 UTC 时区的当前 ISO 8601 时间字符串。

    Returns:
        可直接写入运行状态或报告状态的 UTC 时间字符串。
    """
    return datetime.now(timezone.utc).isoformat()


def create_error_record(
    *,
    stage: str,
    node_name: str,
    category: Literal[
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
    ],
    message: str,
    related_file_id: str | None = None,
    task_id: str | None = None,
    node_execution_id: str | None = None,
    exception_type: str | None = None,
    retryable: bool = False,
    retry_count: int = 0,
    max_retries: int = 0,
    fallback: Literal[
        "skip_file",
        "coordinator",
        "no_memory",
        "default_skill",
        "keep_context",
        "partial_result",
    ]
    | None = None,
    requires_human: bool = False,
    status: Literal[
        "pending",
        "retrying",
        "fallback_applied",
        "waiting_human",
        "recovered",
        "failed",
    ]
    | None = None,
    fatal: bool = False,
    created_at: str | None = None,
    recovered_at: str | None = None,
) -> ErrorRecord:
    """创建具有稳定 ID、且兼容 0.6.0 调用方式的结构化错误记录。

    Args:
        stage: 错误所属的主流程阶段或子图名称。
        node_name: 产生错误的节点函数名。
        category: 文件系统、解析、比较、证据、LLM、校验、协议、Prompt、Hook、
            Memory、Skill、Context Compact、数据库、checkpoint、超时或未知类别。
        message: 可供报告展示的脱敏错误说明。
        related_file_id: 可选关联文件 ID，不应放入原始文件正文。
        task_id: 可选关联 Task ID；Task DAG 创建前可以为 None。
        node_execution_id: 可选节点幂等执行 ID。
        exception_type: 可选已脱敏异常类型名称。
        retryable: 当前错误是否允许自动重试。
        retry_count: 已执行的额外重试次数，不包含第一次正常执行。
        max_retries: 允许执行的最大额外重试次数。
        fallback: 可选安全降级动作。
        requires_human: 自动恢复不足时是否允许请求人工输入。
        status: 可选错误生命周期；省略时按 0.6.0 fatal 语义生成兼容终态。
        fatal: 错误是否使当前治理运行无法安全继续。
        created_at: 可选首次捕获时间；省略时使用当前 UTC 时间。
        recovered_at: 可选完成恢复时间。

    Returns:
        可由 ``merge_by_id`` 合并的 ``ErrorRecord``。

    Raises:
        TypeError: 重试计数、布尔字段或可选字符串类型不正确时抛出。
        ValueError: 重试计数越界、降级动作未知或生命周期状态未知时抛出。
    """
    if isinstance(retry_count, bool) or not isinstance(retry_count, int):
        raise TypeError("retry_count 必须是整数")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise TypeError("max_retries 必须是整数")
    if retry_count < 0 or max_retries < 0:
        raise ValueError("retry_count 和 max_retries 不得为负数")
    if retry_count > max_retries:
        raise ValueError("retry_count 不得大于 max_retries")
    if not isinstance(retryable, bool):
        raise TypeError("retryable 必须是布尔值")
    if not isinstance(requires_human, bool):
        raise TypeError("requires_human 必须是布尔值")
    if fallback is not None and fallback not in RECOVERY_FALLBACK_ACTIONS:
        raise ValueError("fallback 不是允许的安全降级动作")
    if status is not None and status not in ERROR_LIFECYCLE_STATUSES:
        raise ValueError("status 不是允许的错误生命周期状态")
    for field_name, value in (
        ("task_id", task_id),
        ("node_execution_id", node_execution_id),
        ("exception_type", exception_type),
        ("created_at", created_at),
        ("recovered_at", recovered_at),
    ):
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{field_name} 必须是字符串或 None")

    normalized_message = str(message).strip() or "未提供错误说明"
    identity_parts = [
        stage,
        node_name,
        category,
        related_file_id or "",
        normalized_message,
    ]
    if task_id is not None or node_execution_id is not None:
        identity_parts.extend((task_id or "", node_execution_id or ""))
    error_id = hashlib.sha256("\x1f".join(identity_parts).encode()).hexdigest()
    normalized_status = status or ("failed" if fatal else "recovered")
    return ErrorRecord(
        id=error_id,
        stage=stage,
        node_name=node_name,
        category=category,
        exception_type=exception_type,
        message=normalized_message,
        related_file_id=related_file_id,
        task_id=task_id,
        node_execution_id=node_execution_id,
        retryable=retryable,
        retry_count=retry_count,
        max_retries=max_retries,
        fallback=fallback,
        requires_human=requires_human,
        status=normalized_status,
        fatal=fatal,
        created_at=created_at or utc_now_iso(),
        recovered_at=recovered_at,
    )


def paths_overlap(left_path: str | Path, right_path: str | Path) -> bool:
    """判断两个规范化路径是否相同或互为上下级目录。

    Args:
        left_path: 第一个本地路径；路径本身可以尚未创建。
        right_path: 第二个本地路径；路径本身可以尚未创建。

    Returns:
        路径相同或任一路径位于另一路径内部时返回 ``True``。
    """
    left = Path(left_path).expanduser().resolve()
    right = Path(right_path).expanduser().resolve()
    return left == right or left in right.parents or right in left.parents
