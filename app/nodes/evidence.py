from __future__ import annotations

from collections import Counter

from app.services.evidence_matching import (
    EDITABLE_EXTENSIONS,
    match_delivery_log_entries,
)
from app.services.evidence_matching import (
    match_pdf_to_source_version as match_pdf_to_source_version_service,
)
from app.state.models import (
    EvidenceGraphState,
    PdfMatchJob,
    PdfMatchWorkerState,
)
from app.tools.delivery_log import load_local_delivery_log as load_local_delivery_log_tool
from app.utils.error_context import create_node_error
from app.utils.evidence import create_pdf_match_job_id

"""本模块实现独立 Evidence 子图的 PDF 来源与本地发送证据处理节点。"""

def collect_pdf_candidates(state: EvidenceGraphState) -> dict:
    """收集具有标准化内容的非重复 PDF 文件。

    节点只读取图状态中的文件和文档记录，不访问原始文件或内容产物。完全重复
    PDF 不单独建立来源关系，避免同一导出件产生多条等价证据。

    Args:
        state: 已包含文件、文档和版本组的 Evidence 子图状态。

    Returns:
        按文件 ID 稳定排序的 PDF 候选 ID 列表。
    """
    document_file_ids = {item["file_id"] for item in state.get("documents", [])}
    candidate_ids = sorted(
        item["id"]
        for item in state.get("files", [])
        if item["extension"] == ".pdf"
        and item["parse_status"] == "parsed"
        and item["duplicate_of"] is None
        and item["id"] in document_file_ids
    )
    return {"pdf_candidate_ids": candidate_ids}


def create_pdf_match_jobs(state: EvidenceGraphState) -> dict:
    """为每个 PDF 创建限定在同一版本组内的来源匹配任务。

    可编辑来源只允许已解析、具有标准化文档且不是重复件的 XLSX 或 DOCX。
    没有可编辑来源的 PDF 仍会创建空候选任务，由 Worker 生成明确未匹配记录。

    Args:
        state: 已完成 PDF 候选收集的 Evidence 子图状态。

    Returns:
        新建 PDF 任务以及可选的状态引用错误。
    """
    files = list(state.get("files", []))
    file_by_id = {item["id"]: item for item in files}
    document_file_ids = {item["file_id"] for item in state.get("documents", [])}
    groups_by_file: dict[str, list[str]] = {}
    errors = []
    for group in state.get("version_groups", []):
        for file_id in group["file_ids"]:
            if file_id not in file_by_id:
                errors.append(
                    create_node_error(
                        state,
                        stage="evidence",
                        node_name="create_pdf_match_jobs",
                        category="validation",
                        message=f"版本组 {group['id']} 引用未知文件：{file_id}",
                        fatal=True,
                    )
                )
                continue
            groups_by_file.setdefault(file_id, []).append(group["id"])

    jobs: list[PdfMatchJob] = []
    group_by_id = {item["id"]: item for item in state.get("version_groups", [])}
    for pdf_file_id in state.get("pdf_candidate_ids", []):
        group_ids = groups_by_file.get(pdf_file_id, [])
        if len(group_ids) != 1:
            errors.append(
                create_node_error(
                    state,
                    stage="evidence",
                    node_name="create_pdf_match_jobs",
                    category="validation",
                    message=(
                        f"PDF {pdf_file_id} 必须且只能属于一个版本组，"
                        f"实际为 {len(group_ids)} 个"
                    ),
                    related_file_id=pdf_file_id,
                    fatal=True,
                )
            )
            continue
        group_id = group_ids[0]
        source_candidate_ids = sorted(
            file_id
            for file_id in group_by_id[group_id]["file_ids"]
            if file_by_id[file_id]["extension"] in EDITABLE_EXTENSIONS
            and file_by_id[file_id]["parse_status"] == "parsed"
            and file_by_id[file_id]["duplicate_of"] is None
            and file_id in document_file_ids
        )
        jobs.append(
            PdfMatchJob(
                id=create_pdf_match_job_id(group_id, pdf_file_id),
                group_id=group_id,
                pdf_file_id=pdf_file_id,
                source_candidate_ids=source_candidate_ids,
                status="pending",
            )
        )
    return {"pdf_match_jobs": jobs, "errors": errors}


def fanout_pdf_matching(state: EvidenceGraphState) -> dict:
    """把待处理 PDF 匹配任务标记为运行中并交给 Send 路由分发。

    该节点本身不执行匹配；后续 ``dispatch_pdf_match_jobs`` 会为每个运行中任务
    创建独立 Worker 输入，从而避免多个并行任务共享当前任务指针。

    Args:
        state: 已创建 PDF 匹配任务的 Evidence 子图状态。

    Returns:
        状态更新为 ``running`` 的 PDF 匹配任务列表。
    """
    running_jobs = []
    for job in state.get("pdf_match_jobs", []):
        updated_job = dict(job)
        if updated_job["status"] == "pending":
            updated_job["status"] = "running"
        running_jobs.append(PdfMatchJob(**updated_job))
    return {"pdf_match_jobs": running_jobs}


def match_pdf_to_source_version(state: PdfMatchWorkerState) -> dict:
    """执行一个 PDF 来源匹配任务并返回可合并的 Worker 结果。

    节点调用第一批提供的纯匹配服务，不直接读取文件。单个任务的引用或匹配
    错误会被记录为非致命 Evidence 错误，使其他 PDF 和本地发送证据继续处理。

    Args:
        state: 由 Send 路由构造的单个 PDF Worker 状态。

    Returns:
        完成任务和 PDF 来源记录，或失败任务和结构化错误。
    """
    job = PdfMatchJob(**dict(state["job"]))
    try:
        result = match_pdf_to_source_version_service(
            job,
            state.get("files", []),
            state.get("documents", []),
            threshold=float(state["request"]["pdf_match_threshold"]),
        )
        job["status"] = "completed"
        return {"pdf_match_jobs": [job], "pdf_exports": [result]}
    except (KeyError, TypeError, ValueError) as exc:
        job["status"] = "failed"
        return {
            "pdf_match_jobs": [job],
            "errors": [
                create_node_error(
                    state,
                    stage="evidence",
                    node_name="match_pdf_to_source_version",
                    category="evidence",
                    message=str(exc),
                    related_file_id=job["pdf_file_id"],
                    fatal=False,
                )
            ],
        }


def join_pdf_matches(state: EvidenceGraphState) -> dict:
    """在全部并行 Worker 完成后校验 PDF 任务已进入终态。

    Args:
        state: 已合并所有 Worker 输出的 Evidence 子图状态。

    Returns:
        所有任务均已完成或失败时返回空更新，否则返回致命一致性错误。
    """
    nonterminal_jobs = [
        job["id"]
        for job in state.get("pdf_match_jobs", [])
        if job["status"] not in {"completed", "failed"}
    ]
    completed_pdf_ids = {
        job["pdf_file_id"]
        for job in state.get("pdf_match_jobs", [])
        if job["status"] == "completed"
    }
    exported_pdf_ids = {item["pdf_file_id"] for item in state.get("pdf_exports", [])}
    missing_results = sorted(completed_pdf_ids - exported_pdf_ids)
    messages = []
    if nonterminal_jobs:
        messages.append(f"{len(nonterminal_jobs)} 个 PDF 匹配任务未进入终态")
    if missing_results:
        messages.append(f"{len(missing_results)} 个已完成任务缺少 PDF 来源记录")
    if not messages:
        return {}
    return {
        "errors": [
            create_node_error(
                state,
                stage="evidence",
                node_name="join_pdf_matches",
                category="validation",
                message="；".join(messages),
                fatal=True,
            )
        ]
    }


def load_local_delivery_log(state: EvidenceGraphState) -> dict:
    """按请求配置只读加载本地发送记录。

    未提供路径时返回空列表。读取、编码或协议错误会形成非致命 Evidence 错误，
    不阻断已经完成的 PDF 来源匹配，也不会尝试猜测或修复日志内容。

    Args:
        state: 包含可选 ``delivery_log_path`` 的 Evidence 子图状态。

    Returns:
        已校验发送记录，或空列表和结构化非致命错误。
    """
    delivery_log_path = state["request"].get("delivery_log_path")
    if delivery_log_path is None:
        return {"delivery_log_entries": []}
    try:
        entries = load_local_delivery_log_tool(delivery_log_path)
        return {"delivery_log_entries": entries}
    except (OSError, ValueError) as exc:
        return {
            "delivery_log_entries": [],
            "errors": [
                create_node_error(
                    state,
                    stage="evidence",
                    node_name="load_local_delivery_log",
                    category="evidence",
                    message=str(exc),
                    fatal=False,
                )
            ],
        }


def match_delivery_to_version(state: EvidenceGraphState) -> dict:
    """把本地发送记录匹配到唯一文件版本。

    节点只调用确定性纯服务。没有日志时直接返回空更新；状态引用不一致会形成
    致命校验错误，因为此时无法安全解释发送证据属于哪个版本组。

    Args:
        state: 已加载本地发送记录的 Evidence 子图状态。

    Returns:
        本地发送证据匹配结果或结构化一致性错误。
    """
    entries = state.get("delivery_log_entries", [])
    if not entries:
        return {}
    try:
        deliveries = match_delivery_log_entries(
            entries,
            state.get("files", []),
            state.get("documents", []),
            state.get("version_groups", []),
        )
        return {"deliveries": deliveries}
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "errors": [
                create_node_error(
                    state,
                    stage="evidence",
                    node_name="match_delivery_to_version",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            ]
        }


def merge_external_evidence(state: EvidenceGraphState) -> dict:
    """检查本地发送日志与匹配结果是否一一对应。

    PDF 来源和发送证据分别保存在类型安全的 reducer 列表中，本节点不把两种
    记录压成无类型对象，只验证每条已加载日志都产生了一个本地匹配结果。

    Args:
        state: 已完成 PDF 和发送记录匹配的 Evidence 子图状态。

    Returns:
        证据数量一致时返回空更新，否则返回致命校验错误。
    """
    expected_refs = Counter(
        item["evidence_ref"] for item in state.get("delivery_log_entries", [])
    )
    actual_refs = Counter(
        item["evidence_ref"]
        for item in state.get("deliveries", [])
        if item["evidence_source"] == "local_log"
        and item["evidence_ref"] in expected_refs
    )
    if expected_refs == actual_refs:
        return {}
    return {
        "errors": [
            create_node_error(
                state,
                stage="evidence",
                node_name="merge_external_evidence",
                category="validation",
                message="本地发送日志与匹配结果数量或引用不一致",
                fatal=True,
            )
        ]
    }


def validate_evidence_confidence(state: EvidenceGraphState) -> dict:
    """验证 Evidence 子图输出的引用关系、分数范围和匹配约束。

    未匹配或低置信度本身是合法业务结果，不会被记录为错误；只有未知文件、
    跨版本组来源、非法分数或自相矛盾的匹配状态才形成致命校验错误。

    Args:
        state: 已合并全部外部证据的 Evidence 子图状态。

    Returns:
        输出合法时返回空更新，否则返回结构化致命错误列表。
    """
    file_by_id = {item["id"]: item for item in state.get("files", [])}
    group_by_id = {item["id"]: item for item in state.get("version_groups", [])}
    group_by_file: dict[str, str] = {}
    messages = []
    for group in state.get("version_groups", []):
        for file_id in group["file_ids"]:
            if file_id not in file_by_id:
                messages.append(f"版本组 {group['id']} 引用未知文件 {file_id}")
            elif file_id in group_by_file:
                messages.append(f"文件 {file_id} 同时属于多个版本组")
            else:
                group_by_file[file_id] = group["id"]

    try:
        threshold = float(state["request"].get("pdf_match_threshold", -1))
    except (TypeError, ValueError):
        threshold = -1.0
    if not 0.0 <= threshold <= 1.0:
        messages.append("pdf_match_threshold 必须位于 0.0 到 1.0 之间")
    for export in state.get("pdf_exports", []):
        pdf_file = file_by_id.get(export["pdf_file_id"])
        if pdf_file is None or pdf_file["extension"] != ".pdf":
            messages.append(f"PDF 来源记录 {export['id']} 引用未知或非 PDF 文件")
        if export["group_id"] not in group_by_id:
            messages.append(f"PDF 来源记录 {export['id']} 引用未知版本组")
        if group_by_file.get(export["pdf_file_id"]) != export["group_id"]:
            messages.append(f"PDF 来源记录 {export['id']} 的 PDF 与版本组不一致")
        if not 0.0 <= export["match_score"] <= 1.0:
            messages.append(f"PDF 来源记录 {export['id']} 的 match_score 非法")
        if not 0.0 <= export["confidence"] <= 1.0:
            messages.append(f"PDF 来源记录 {export['id']} 的 confidence 非法")
        source_file_id = export["source_file_id"]
        if source_file_id is not None:
            source_file = file_by_id.get(source_file_id)
            if source_file is None or source_file["extension"] not in EDITABLE_EXTENSIONS:
                messages.append(f"PDF 来源记录 {export['id']} 引用未知或不可编辑来源")
            if group_by_file.get(source_file_id) != export["group_id"]:
                messages.append(f"PDF 来源记录 {export['id']} 的来源跨越版本组")
            if export["match_score"] < threshold:
                messages.append(f"PDF 来源记录 {export['id']} 未达到匹配阈值")

    for delivery in state.get("deliveries", []):
        if not 0.0 <= delivery["confidence"] <= 1.0:
            messages.append(f"发送证据 {delivery['id']} 的 confidence 非法")
        file_id = delivery["file_id"]
        group_id = delivery["group_id"]
        if file_id is None:
            if group_id is not None or delivery["match_method"] != "unmatched":
                messages.append(f"未匹配发送证据 {delivery['id']} 包含文件或版本组")
            if delivery["confidence"] != 0.0:
                messages.append(f"未匹配发送证据 {delivery['id']} 的置信度必须为零")
            continue
        if file_id not in file_by_id:
            messages.append(f"发送证据 {delivery['id']} 引用未知文件")
        if group_by_file.get(file_id) != group_id:
            messages.append(f"发送证据 {delivery['id']} 的文件与版本组不一致")
        if delivery["match_method"] == "unmatched":
            messages.append(f"已匹配发送证据 {delivery['id']} 使用 unmatched 方法")

    if not messages:
        return {}
    return {
        "errors": [
            create_node_error(
                state,
                stage="evidence",
                node_name="validate_evidence_confidence",
                category="validation",
                message=message,
                fatal=True,
            )
            for message in dict.fromkeys(messages)
        ]
    }
