from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from app.state.models import BackgroundJobState

"""本模块把后台报告路径限制在任务声明的报告根目录内，并执行下载前校验。"""


# HTTP 下载接口当前只公开项目生成的 Markdown 报告。
ALLOWED_REPORT_SUFFIXES = frozenset({".md", ".markdown"})

# 单个报告允许由 API 直接返回的最大文件大小。
MAX_REPORT_DOWNLOAD_BYTES = 16 * 1024 * 1024


def resolve_safe_report_path(job: BackgroundJobState) -> Path:
    """解析并校验后台任务可供 HTTP 下载的报告文件。

    本函数不是 LLM Tool，不生成报告也不读取正文。它会同时解析报告根目录与
    最终文件路径，拒绝路径越界、符号链接逃逸、非 Markdown 文件和超大文件。

    Args:
        job: 包含规范化工作区信封和可选报告路径的后台任务状态。

    Returns:
        已确认存在、位于报告根目录内且符合下载限制的绝对文件路径。

    Raises:
        FileNotFoundError: 报告尚未生成、根目录或文件不存在时抛出。
        ValueError: 请求信封损坏、路径越界、文件类型或大小不安全时抛出。
        OSError: 文件系统拒绝检查路径或元数据时抛出。
    """
    report_path = job.get("report_path")
    if not isinstance(report_path, str) or not report_path.strip():
        raise FileNotFoundError("当前运行尚未生成可下载报告")
    workspace = job.get("request_payload", {}).get("workspace")
    if not isinstance(workspace, Mapping):
        raise ValueError("后台任务缺少受控 workspace 配置")
    report_root_value = workspace.get("report_root")
    if not isinstance(report_root_value, str) or not report_root_value.strip():
        raise ValueError("后台任务缺少受控 workspace.report_root")

    report_root = Path(report_root_value).expanduser().resolve(strict=True)
    candidate = Path(report_path).expanduser()
    if candidate.is_symlink():
        raise ValueError("报告文件不得是符号链接")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(report_root):
        raise ValueError("报告路径越过当前任务的 report_root")
    if not resolved.is_file():
        raise FileNotFoundError("报告路径不是可下载文件")
    if resolved.suffix.lower() not in ALLOWED_REPORT_SUFFIXES:
        raise ValueError("报告文件类型不允许通过 HTTP 下载")
    if resolved.stat().st_size > MAX_REPORT_DOWNLOAD_BYTES:
        raise ValueError("报告文件超过 HTTP 下载大小上限")
    return resolved
