from __future__ import annotations

from app.services.reporting import build_report_state, escape_markdown_cell
from app.state.models import FileGovernanceState

"""本模块实现失败、无数据及包含版本推荐与外部证据的治理报告节点。"""


def generate_failure_report(state: FileGovernanceState) -> dict:
    """根据致命错误生成失败报告，并保留已获得的部分事实。"""
    errors = state.get("errors", [])
    lines = [
        "# 文件版本治理失败报告",
        "",
        f"运行 ID：`{state['run']['run_id']}`",
        "",
        "## 错误",
        "",
    ]
    if errors:
        lines.extend(
            f"- `{error['node_name']}`：{error['message']}"
            for error in errors
        )
    else:
        lines.append("- 未记录到结构化错误，请检查运行日志。")
    summary = "文件版本治理未能安全完成。"
    markdown = "\n".join(lines)
    warnings = [error["message"] for error in errors]
    return {"report": build_report_state(state, summary, markdown, warnings)}


def generate_no_data_report(state: FileGovernanceState) -> dict:
    """在没有可分析文档时生成文件统计和解析警告报告。"""
    files = state.get("files", [])
    errors = state.get("errors", [])
    status_counts: dict[str, int] = {}
    for file_record in files:
        status = file_record["parse_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    lines = [
        "# 文件版本治理报告",
        "",
        "未发现可用于版本分析的标准化文档。",
        "",
        "## 文件状态统计",
        "",
    ]
    if status_counts:
        lines.extend(f"- {status}：{count}" for status, count in sorted(status_counts.items()))
    else:
        lines.append("- 扫描范围内没有匹配文件。")
    if errors:
        lines.extend(["", "## 警告", ""])
        lines.extend(f"- {error['message']}" for error in errors)
    summary = "没有可分析文档，未执行版本推荐。"
    markdown = "\n".join(lines)
    warnings = [error["message"] for error in errors]
    return {"report": build_report_state(state, summary, markdown, warnings)}


def generate_governance_report(state: FileGovernanceState) -> dict:
    """生成包含版本链、证据、候选评分和主版本选择的治理报告。

    PDF 来源和发送记录只展示结构化匹配结果、脱敏收件人标签及稳定证据引用，
    不读取原始附件、完整正文或外部系统凭据。未匹配发送记录单独列出，避免被
    误解为已经支持某个主版本建议。

    Args:
        state: 已完成 Recommendation 或人工选择阶段的顶层治理状态。

    Returns:
        包含 Markdown 正文、摘要、警告和持久化路径的报告状态更新。
    """
    file_by_id = {item["id"]: item for item in state.get("files", [])}
    chain_by_group = {
        item["group_id"]: item for item in state.get("version_chains", [])
    }
    decision_by_group = {
        item["group_id"]: item for item in state.get("decisions", [])
    }
    branches_by_group: dict[str, list] = {}
    for branch in state.get("branches", []):
        branches_by_group.setdefault(branch["group_id"], []).append(branch)
    pdf_exports_by_group: dict[str, list] = {}
    for pdf_export in state.get("pdf_exports", []):
        pdf_exports_by_group.setdefault(pdf_export["group_id"], []).append(pdf_export)
    deliveries_by_group: dict[str, list] = {}
    unmatched_deliveries = []
    for delivery in state.get("deliveries", []):
        if delivery["group_id"] is None:
            unmatched_deliveries.append(delivery)
        else:
            deliveries_by_group.setdefault(delivery["group_id"], []).append(delivery)

    lines = [
        "# 文件版本治理报告",
        "",
        f"运行 ID：`{state['run']['run_id']}`",
        "",
        f"共识别 {len(state.get('version_groups', []))} 个文档版本组。",
    ]
    for group in state.get("version_groups", []):
        chain = chain_by_group[group["id"]]
        decision = decision_by_group[group["id"]]
        lines.extend(
            [
                "",
                f"## {escape_markdown_cell(group['label'])}",
                "",
                f"分组置信度：{group['confidence']:.2f}",
                "",
                "### 版本链",
                "",
            ]
        )
        for index, file_id in enumerate(chain["ordered_file_ids"], start=1):
            file_record = file_by_id[file_id]
            markers = []
            if file_id in chain["leaf_file_ids"]:
                markers.append("叶子版本")
            if file_record["duplicate_of"]:
                markers.append("完全重复件")
            suffix = f"（{'、'.join(markers)}）" if markers else ""
            lines.append(f"{index}. `{file_record['file_name']}`{suffix}")

        lines.extend(
            [
                "",
                "### 候选评分",
                "",
                "| 文件 | 评分 |",
                "|---|---:|",
            ]
        )
        for file_id, score in sorted(
            decision["candidate_scores"].items(),
            key=lambda item: item[1],
            reverse=True,
        ):
            lines.append(
                f"| {escape_markdown_cell(file_by_id[file_id]['file_name'])} | {score:.2f} |"
            )
        recommended_id = decision["recommended_file_id"]
        recommended_name = (
            file_by_id[recommended_id]["file_name"] if recommended_id else "未选择"
        )
        lines.extend(
            [
                "",
                f"推荐主版本：`{recommended_name}`",
                f"选择方式：`{decision['selected_by']}`",
                f"推荐置信度：{decision['confidence']:.2f}",
            ]
        )
        if branches_by_group.get(group["id"]):
            lines.append(f"版本分叉：{len(branches_by_group[group['id']])} 个")
        if decision["reasons"]:
            lines.extend(["", "推荐理由："])
            lines.extend(f"- {reason}" for reason in decision["reasons"])
        if chain["warnings"]:
            lines.extend(["", "版本链警告："])
            lines.extend(f"- {warning}" for warning in chain["warnings"])

        lines.extend(["", "### PDF 来源证据", ""])
        group_pdf_exports = pdf_exports_by_group.get(group["id"], [])
        if group_pdf_exports:
            lines.extend(
                [
                    "| PDF | 可编辑来源 | 匹配分 | 置信度 | 匹配信号 |",
                    "|---|---|---:|---:|---|",
                ]
            )
            for pdf_export in group_pdf_exports:
                pdf_file = file_by_id.get(pdf_export["pdf_file_id"])
                source_file_id = pdf_export["source_file_id"]
                source_file = file_by_id.get(source_file_id) if source_file_id else None
                pdf_name = pdf_file["file_name"] if pdf_file else pdf_export["pdf_file_id"]
                source_name = source_file["file_name"] if source_file else "未可靠匹配"
                signals = "；".join(pdf_export["matched_signals"]) or "无"
                lines.append(
                    "| "
                    f"{escape_markdown_cell(pdf_name)} | "
                    f"{escape_markdown_cell(source_name)} | "
                    f"{pdf_export['match_score']:.2f} | "
                    f"{pdf_export['confidence']:.2f} | "
                    f"{escape_markdown_cell(signals)} |"
                )
        else:
            lines.append("- 当前版本组没有 PDF 来源记录。")

        lines.extend(["", "### 发送与确认记录", ""])
        group_deliveries = deliveries_by_group.get(group["id"], [])
        if group_deliveries:
            lines.extend(
                [
                    "| 文件 | 收件人 | 发送时间 | 客户确认 | 匹配方式 | 置信度 | 证据引用 |",
                    "|---|---|---|---|---|---:|---|",
                ]
            )
            for delivery in group_deliveries:
                delivered_file = file_by_id.get(delivery["file_id"])
                delivered_name = (
                    delivered_file["file_name"]
                    if delivered_file
                    else delivery["file_id"] or "未匹配"
                )
                lines.append(
                    "| "
                    f"{escape_markdown_cell(delivered_name)} | "
                    f"{escape_markdown_cell(delivery['recipient_label'])} | "
                    f"{escape_markdown_cell(delivery['sent_at'] or '未知')} | "
                    f"{'是' if delivery['customer_confirmed'] else '否'} | "
                    f"{delivery['match_method']} | "
                    f"{delivery['confidence']:.2f} | "
                    f"{escape_markdown_cell(delivery['evidence_ref'])} |"
                )
        else:
            lines.append("- 当前版本组没有已匹配的发送记录。")

    if unmatched_deliveries:
        lines.extend(
            [
                "",
                "## 未匹配发送证据",
                "",
                "以下记录未可靠关联到具体文件版本，不参与自动推荐加权。",
                "",
                "| 收件人 | 发送时间 | 客户确认 | 证据引用 |",
                "|---|---|---|---|",
            ]
        )
        for delivery in unmatched_deliveries:
            lines.append(
                "| "
                f"{escape_markdown_cell(delivery['recipient_label'])} | "
                f"{escape_markdown_cell(delivery['sent_at'] or '未知')} | "
                f"{'是' if delivery['customer_confirmed'] else '否'} | "
                f"{escape_markdown_cell(delivery['evidence_ref'])} |"
            )

    errors = state.get("errors", [])
    if errors:
        lines.extend(["", "## 运行警告", ""])
        lines.extend(f"- `{error['node_name']}`：{error['message']}" for error in errors)
    lines.extend(
        [
            "",
            "## 保留策略",
            "",
            "本工具不删除、移动、重命名或覆盖原始文件；所有版本链文件均继续保留。",
        ]
    )
    summary = (
        f"完成 {len(state.get('version_groups', []))} 个文档组的版本治理，"
        f"产生 {len(state.get('decisions', []))} 个主版本结果、"
        f"{len(state.get('pdf_exports', []))} 条 PDF 来源记录和"
        f"{len(state.get('deliveries', []))} 条发送证据。"
    )
    markdown = "\n".join(lines)
    warnings = [error["message"] for error in errors]
    return {"report": build_report_state(state, summary, markdown, warnings)}
