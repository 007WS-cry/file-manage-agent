from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.state.models import ErrorRecord

"""本模块提供运行时间、结构化错误和本地路径关系判断等通用辅助函数。"""


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
        "unknown",
    ],
    message: str,
    related_file_id: str | None = None,
    fatal: bool = False,
) -> ErrorRecord:
    """创建具有稳定 ID 的结构化节点错误记录。

    Args:
        stage: 错误所属的主流程阶段或子图名称。
        node_name: 产生错误的节点函数名。
        category: 文件系统、解析、比较、证据、LLM、校验、协议、Prompt、Hook、
            Memory 或未知类别。
        message: 可供报告展示的脱敏错误说明。
        related_file_id: 可选关联文件 ID，不应放入原始文件正文。
        fatal: 错误是否使当前治理运行无法安全继续。

    Returns:
        可由 ``merge_by_id`` 合并的 ``ErrorRecord``。
    """
    normalized_message = str(message).strip() or "未提供错误说明"
    error_id = hashlib.sha256(
        "\x1f".join(
            (
                stage,
                node_name,
                category,
                related_file_id or "",
                normalized_message,
            )
        ).encode()
    ).hexdigest()
    return ErrorRecord(
        id=error_id,
        stage=stage,
        node_name=node_name,
        category=category,
        message=normalized_message,
        related_file_id=related_file_id,
        fatal=fatal,
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
