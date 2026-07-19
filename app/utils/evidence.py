from __future__ import annotations

import hashlib

"""本模块提供 Evidence 子图使用的无副作用通用辅助函数。"""


def create_pdf_match_job_id(group_id: str, pdf_file_id: str) -> str:
    """根据版本组和 PDF 文件生成稳定的匹配任务 ID。

    Args:
        group_id: PDF 所属版本组的稳定 ID。
        pdf_file_id: 等待匹配来源的 PDF 文件稳定 ID。

    Returns:
        带 ``pdf-job`` 前缀的稳定 SHA-256 任务 ID。
    """
    digest = hashlib.sha256(f"{group_id}\x1f{pdf_file_id}".encode()).hexdigest()
    return f"pdf-job:{digest}"
