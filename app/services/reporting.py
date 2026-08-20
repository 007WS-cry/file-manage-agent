from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.state.models import FileGovernanceState, ReportState
from app.utils.error_context import is_error_unresolved
from app.utils.runtime import paths_overlap, utc_now_iso

"""本模块负责版本摘要 Markdown、值转义、隔离持久化和统一报告状态构造。"""


SEMANTIC_CHANGE_LABELS = {
    "amount": "金额变化",
    "date_or_term": "日期或期限变化",
    "responsible_party": "责任主体变化",
    "delivery_scope": "交付范围变化",
    "payment_term": "付款条件变化",
    "breach_liability": "违约责任变化",
    "approval_status": "审批状态变化",
    "contact_or_recipient": "联系人或收件人变化",
    "wording_only": "纯措辞优化",
    "formatting_only": "格式调整",
    "no_material_change": "无实质变化",
}
# 语义变更类型到中文报告标签的固定映射。

REVIEW_PRIORITY_LABELS = {
    "not_assessed": "未评估",
    "low": "低",
    "medium": "中",
    "high": "高",
}
# 审核优先级到中文报告标签的固定映射。

VERSION_RELATION_LABELS = {
    "direct_revision": "直接修订",
    "parallel_branch": "并行分支",
    "derived_export": "导出版本",
    "semantic_duplicate": "语义重复",
    "unrelated": "无关文件",
    "uncertain": "无法判断",
}
# 双轨版本关系类型到中文报告标签的固定映射。

VERSION_RELATION_RESOLUTION_LABELS = {
    "deterministic_only": "仅确定性结果",
    "consensus": "双轨一致",
    "llm_supported": "约束内采纳 LLM 候选",
    "conflict_review": "双轨冲突待复核",
    "constrained_rejection": "候选违反约束待复核",
}
# 双轨关系融合方式到中文报告标签的固定映射。


def escape_markdown_cell(value: object) -> str:
    """转义 Markdown 表格单元格中的竖线和换行。

    Args:
        value: 将要显示在 Markdown 表格中的任意标量值。

    Returns:
        不会破坏表格列或额外生成换行的文本。
    """
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def build_recovery_report_lines(state: FileGovernanceState) -> list[str]:
    """生成彼此独立的“已恢复错误”和“降级项”Markdown 章节。

    已恢复错误包含正常重试恢复和已应用安全降级的错误生命周期记录；降级项则
    单独展示动作、影响和受影响文件，避免把部分完成误解为执行失败。没有任何
    恢复事实时不生成空章节。

    Args:
        state: 包含结构化错误和降级记录的顶层治理状态。

    Returns:
        可直接追加到报告正文的 Markdown 行。
    """
    recovered_error_by_id = {
        str(error["id"]): error
        for error in state.get("errors", [])
        if error.get("status") in {"recovered", "fallback_applied"}
    }
    recovered_errors = sorted(
        recovered_error_by_id.values(),
        key=lambda item: (str(item.get("created_at", "")), str(item.get("id", ""))),
    )
    degradation_by_id = {
        str(degradation["id"]): degradation
        for degradation in state.get("degradations", [])
    }
    degradations = sorted(
        degradation_by_id.values(),
        key=lambda item: (str(item.get("created_at", "")), str(item.get("id", ""))),
    )
    lines: list[str] = []
    if recovered_errors:
        lines.extend(["", "## 已恢复错误", ""])
        for error in recovered_errors:
            retry_count = int(error.get("retry_count", 0))
            status_label = (
                "安全降级已应用"
                if error.get("status") == "fallback_applied"
                else "重试或结果复用后恢复"
            )
            task_id = error.get("task_id") or "未绑定 Task"
            lines.append(
                "- `"
                f"{escape_markdown_cell(error.get('node_name', 'unknown'))}`"
                f"（{escape_markdown_cell(error.get('stage', 'unknown'))}，"
                f"{status_label}，重试 {retry_count} 次，"
                f"Task `{escape_markdown_cell(task_id)}`）："
                f"{escape_markdown_cell(error.get('message', '未提供错误说明'))}"
            )

    if degradations:
        lines.extend(["", "## 降级项", ""])
        for degradation in degradations:
            affected_file_ids = list(degradation.get("affected_file_ids", []))
            affected_files = (
                "、".join(
                    f"`{escape_markdown_cell(file_id)}`"
                    for file_id in affected_file_ids
                )
                if affected_file_ids
                else "无特定文件"
            )
            lines.extend(
                [
                    "- `"
                    f"{escape_markdown_cell(degradation.get('action', 'unknown'))}`"
                    f"（{escape_markdown_cell(degradation.get('stage', 'unknown'))}）："
                    f"{escape_markdown_cell(degradation.get('summary', '已应用安全降级。'))}",
                    "  - 影响："
                    f"{escape_markdown_cell(degradation.get('impact', '结果完整性可能降低。'))}",
                    f"  - 受影响文件：{affected_files}",
                ]
            )
    return lines


def append_recovery_outcome_summary(
    state: FileGovernanceState,
    summary: str,
) -> str:
    """为非失败报告追加恢复数量和部分完成语义。

    Args:
        state: 包含错误生命周期和降级记录的顶层治理状态。
        summary: 报告节点生成的基础摘要。

    Returns:
        失败状态保持原摘要；存在已恢复错误时明确标注部分完成的摘要。
    """
    if any(is_error_unresolved(error) for error in state.get("errors", [])):
        return summary
    recovered_count = len(
        {
            str(error["id"])
            for error in state.get("errors", [])
            if error.get("status") in {"recovered", "fallback_applied"}
        }
    )
    degradation_count = len(
        {
            str(degradation["id"])
            for degradation in state.get("degradations", [])
        }
    )
    if recovered_count == 0 and degradation_count == 0:
        return summary
    suffix = f"本次运行已恢复 {recovered_count} 个错误"
    if degradation_count:
        suffix += f"，并记录 {degradation_count} 个降级项"
    return f"{summary.rstrip()} {suffix}，结果为部分完成。"


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
        file_record["id"]: file_record["file_name"] for file_record in state.get("files", [])
    }
    diffs = sorted(
        (diff for diff in state.get("diffs", []) if diff.get("group_id") == group_id),
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
            "Version Subagent" if diff.get("summary_source") == "version_subagent" else "确定性规则"
        )
        lines.append(
            "- `"
            f"{escape_markdown_cell(left_name)}` ↔ `"
            f"{escape_markdown_cell(right_name)}`（{source}）："
            f"{escape_markdown_cell(diff['summary'])}"
        )
        deterministic_relation = diff.get("deterministic_relation", "uncertain")
        llm_relation = diff.get("llm_relation")
        resolved_relation = diff.get(
            "resolved_relation",
            deterministic_relation,
        )
        relation_resolution = diff.get(
            "relation_resolution",
            "deterministic_only",
        )
        lines.append(
            "  - 双轨版本关系：确定性="
            f"**{VERSION_RELATION_LABELS.get(deterministic_relation, escape_markdown_cell(deterministic_relation))}**；"
            "LLM="
            f"**{VERSION_RELATION_LABELS.get(llm_relation['relation'], escape_markdown_cell(llm_relation['relation'])) if llm_relation else '未提出'}**；"
            "最终="
            f"**{VERSION_RELATION_LABELS.get(resolved_relation, escape_markdown_cell(resolved_relation))}**；"
            f"融合={VERSION_RELATION_RESOLUTION_LABELS.get(relation_resolution, escape_markdown_cell(relation_resolution))}；"
            f"置信度 {float(diff.get('relation_confidence', 0.0)):.2f}"
        )
        if diff.get("relation_review_required", False):
            reasons = "；".join(diff.get("relation_review_reasons", []))
            lines.append(
                "  - **版本关系需要人工审核**："
                f"{escape_markdown_cell(reasons or '双轨关系存在待复核问题')}"
            )
        priority = diff.get("review_priority", "not_assessed")
        lines.append(
            "  - 语义审核优先级："
            f"**{REVIEW_PRIORITY_LABELS.get(priority, escape_markdown_cell(priority))}**"
        )
        semantic_changes = diff.get("semantic_changes", [])
        if not semantic_changes:
            lines.append("  - 语义变更：未生成经过证据校验的业务分类。")
        for change in semantic_changes:
            change_label = SEMANTIC_CHANGE_LABELS.get(
                change["change_type"],
                change["change_type"],
            )
            old_value = change.get("old_value") or "未提供"
            new_value = change.get("new_value") or "未提供"
            refs = "、".join(
                f"`{escape_markdown_cell(item)}`"
                for item in change.get("evidence_refs", [])
            )
            lines.append(
                "  - "
                f"{escape_markdown_cell(change_label)} / "
                f"{escape_markdown_cell(change['significance'])} "
                f"（置信度 {change['confidence']:.2f}）："
                f"{escape_markdown_cell(old_value)} → {escape_markdown_cell(new_value)}；"
                f"影响：{escape_markdown_cell(change['business_impact'])}；"
                f"证据：{refs}"
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
        包含摘要、Markdown、警告、恢复索引、可选磁盘路径和生成时间的报告状态。
    """
    merged_warnings = list(dict.fromkeys(warnings))
    degradation_ids = list(
        dict.fromkeys(item["id"] for item in state.get("degradations", []))
    )
    recovered_error_ids = [
        error_id
        for error_id in dict.fromkeys(
            item["id"]
            for item in state.get("errors", [])
            if item.get("status") in {"recovered", "fallback_applied"}
        )
    ]
    try:
        report_path = persist_report(state, markdown)
    except (OSError, ValueError) as exc:
        report_path = None
        merged_warnings.append(f"报告未写入磁盘：{exc}")
    return ReportState(
        summary=append_recovery_outcome_summary(state, summary),
        report_markdown=markdown,
        warnings=merged_warnings,
        report_path=report_path,
        generated_at=utc_now_iso(),
        degradation_ids=degradation_ids,
        recovered_error_ids=recovered_error_ids,
    )
