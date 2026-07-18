from __future__ import annotations

from collections.abc import Iterable

from app.state.models import ComparisonJob, FileRecord

"""本模块提供按稳定 ID 查询状态记录的无副作用辅助函数。"""


def find_file_by_id(
    files: Iterable[FileRecord],
    file_id: str | None,
) -> FileRecord | None:
    """从文件记录集合中查找指定 ID 的文件。

    Args:
        files: 顶层状态或 Inventory 子图中的文件记录集合。
        file_id: 待查找的稳定文件 ID；为 ``None`` 时不会匹配任何记录。

    Returns:
        首个 ID 匹配的文件记录；不存在时返回 ``None``。
    """
    return next((item for item in files if item["id"] == file_id), None)


def find_comparison_job_by_id(
    jobs: Iterable[ComparisonJob],
    job_id: str | None,
) -> ComparisonJob | None:
    """从比较任务集合中查找指定 ID 的任务。

    Args:
        jobs: Version Analysis 子图中的比较任务集合。
        job_id: 待查找的稳定任务 ID；为 ``None`` 时不会匹配任何记录。

    Returns:
        首个 ID 匹配的比较任务；不存在时返回 ``None``。
    """
    return next((item for item in jobs if item["id"] == job_id), None)
