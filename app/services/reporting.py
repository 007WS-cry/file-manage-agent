from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.state.models import FileGovernanceState, ReportState
from app.utils.runtime import paths_overlap, utc_now_iso

"""本模块负责版本摘要 Markdown、值转义、隔离持久化和统一报告状态构造。"""


def escape_markdown_cell(value: object) -> str:
    """转义 Markdown 表格单元格中的竖线和换行。

    Args:
        value: 将要显示在 Markdown 表格中的任意标量值。

    Returns:
        不会破坏表格列或额外生成换行的文本。
    """
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def build_version_summary_lines(
    state: FileGovernanceState,
    group_id: str,
) -> list[str]:
    """生成一个版本组的确定性或 Version Subagent 摘要报告行。

    Args:
        state: 包含文件索引和已完成差异记录的顶层治理状态。
        group_id: 等待展示关键修改摘要的版本组 ID。

    Returns:
        可直接追加到 Markdown 报告的章节行；没有文件对差异时返回明确说明。
    """
    file_names = {
        file_record["id"]: file_record["file_name"]
        for file_record in state.get("files", [])
    }
    diffs = sorted(
        (
            diff
            for diff in state.get("diffs", [])
            if diff.get("group_id") == group_id
        ),
        key=lambda item: item["id"],
    )
    lines = ["", "### 关键修改摘要", ""]
    if not diffs:
        lines.append("- 当前版本组没有需要展示的文件对差异。")
        return lines

    for diff in diffs:
        left_name = file_names.get(diff["file_a_id"], diff["file_a_id"])
        right_name = file_names.get(diff["file_b_id"], diff["file_b_id"])
        source = (
            "Version Subagent"
            if diff.get("summary_source") == "version_subagent"
            else "确定性规则"
        )
        lines.append(
            "- `"
            f"{escape_markdown_cell(left_name)}` ↔ `"
            f"{escape_markdown_cell(right_name)}`（{source}）："
            f"{escape_markdown_cell(diff['summary'])}"
        )
        message_id = diff.get("summary_message_id")
        if message_id:
            lines.append(f"  - Team Message：`{escape_markdown_cell(message_id)}`")
        artifact_ref = diff.get("summary_artifact_ref")
        if artifact_ref:
            lines.append(f"  - 解释引用：`{escape_markdown_cell(artifact_ref)}`")
    return lines


def persist_report(state: FileGovernanceState, markdown: str) -> str:
    """把报告原子写入只读输入目录之外，并返回绝对路径。

    函数只写入状态中已经过顶层请求校验的 ``report_root``，并在写入前再次
    校验报告目录与输入目录互不重叠。写入采用同目录临时文件和原子替换，既不
    修改任何输入业务文件，也不执行来自报告内容的命令或代码。

    Args:
        state: 包含已校验工作空间和运行 ID 的顶层治理状态。
        markdown: 将要持久化的完整 Markdown 报告文本。

    Returns:
        已写入报告文件的绝对路径字符串。

    Raises:
        OSError: 报告目录创建、临时文件写入或原子替换失败。
        ValueError: 报告目录与只读输入目录相同或互为上下级目录。
    """
    report_root = Path(state["workspace"]["report_root"]).expanduser().resolve()
    input_root = Path(state["workspace"]["input_root"]).expanduser().resolve(strict=True)
    if paths_overlap(input_root, report_root):
        raise ValueError("报告目录与只读输入目录不得相同或互为上下级目录")
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / f"{state['run']['run_id']}.md"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".tmp",
            prefix=f"{state['run']['run_id']}.",
            dir=report_root,
            delete=False,
        ) as stream:
            stream.write(markdown)
            stream.write("\n")
            temporary_path = Path(stream.name)
        os.replace(temporary_path, report_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return str(report_path)


def build_report_state(
    state: FileGovernanceState,
    summary: str,
    markdown: str,
    warnings: list[str],
) -> ReportState:
    """构造统一报告状态，并在磁盘写入失败时保留内存报告。

    Args:
        state: 包含运行信息和已校验工作空间的顶层治理状态。
        summary: 面向调用方的报告摘要。
        markdown: 完整 Markdown 报告文本。
        warnings: 已知运行警告列表。

    Returns:
        包含摘要、Markdown、警告、可选磁盘路径和生成时间的报告状态。
    """
    merged_warnings = list(warnings)
    try:
        report_path = persist_report(state, markdown)
    except (OSError, ValueError) as exc:
        report_path = None
        merged_warnings.append(f"报告未写入磁盘：{exc}")
    return ReportState(
        summary=summary,
        report_markdown=markdown,
        warnings=merged_warnings,
        report_path=report_path,
        generated_at=utc_now_iso(),
    )
