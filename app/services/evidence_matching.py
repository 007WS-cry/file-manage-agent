from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from app.services.document_grouping import (
    calculate_key_field_similarity,
    calculate_text_similarity,
    filenames_share_topic,
    normalize_filename_stem,
)
from app.state.models import (
    DeliveryLogEntry,
    DeliveryRecord,
    DocumentRecord,
    FileRecord,
    PdfExportRecord,
    PdfMatchJob,
    VersionGroupRecord,
)

"""本模块提供不执行文件读写的 PDF 来源和本地发送证据纯匹配规则。"""


# 可以作为 PDF 导出来源和主版本候选的可编辑文件扩展名。
EDITABLE_EXTENSIONS = frozenset({".xlsx", ".docx"})
# 默认判定 PDF 来源匹配成功所需的最低综合分数。
DEFAULT_PDF_MATCH_THRESHOLD = 0.82
# 前两名 PDF 来源候选必须达到的最小分差，防止近似并列时自动选择。
MIN_PDF_MATCH_MARGIN = 0.05
# 单次文本相似度计算允许使用的最大预览字符数。
MAX_MATCH_PREVIEW_CHARACTERS = 20_000


def _stable_record_id(prefix: str, *parts: str) -> str:
    """根据业务主键生成适合 reducer 合并的稳定记录 ID。

    Args:
        prefix: 表示记录类型的可读前缀。
        parts: 能唯一标识业务记录的稳定字符串片段。

    Returns:
        由前缀和 SHA-256 摘要组成的记录 ID。
    """
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _ensure_unique_ids(records: Iterable[dict], *, label: str) -> None:
    """校验一组状态记录不存在重复 ID。

    Args:
        records: 包含 ``id`` 字段的状态记录。
        label: 用于错误信息的记录类型名称。

    Raises:
        ValueError: 记录缺少 ID 或存在重复 ID 时抛出。
    """
    record_ids: list[str] = []
    for record in records:
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(f"{label}存在空 id")
        record_ids.append(record_id)
    if len(set(record_ids)) != len(record_ids):
        raise ValueError(f"{label}存在重复 id")


def _is_source_time_plausible(source_time: str, pdf_time: str) -> bool:
    """判断可编辑源文件的修改时间是否不晚于 PDF。

    Args:
        source_time: 可编辑源文件的 ISO 8601 修改时间。
        pdf_time: PDF 文件的 ISO 8601 修改时间。

    Returns:
        两个时间均可解析且源文件不晚于 PDF 时返回 ``True``。
    """
    try:
        source_value = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
        pdf_value = datetime.fromisoformat(pdf_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    if source_value.tzinfo is None or pdf_value.tzinfo is None:
        return False
    return source_value <= pdf_value


def _score_pdf_source_candidate(
    pdf_file: FileRecord,
    source_file: FileRecord,
    pdf_document: DocumentRecord | None,
    source_document: DocumentRecord | None,
) -> tuple[float, list[str]]:
    """计算一个 PDF 与单个可编辑来源候选的综合匹配分数。

    Args:
        pdf_file: 等待判断来源的 PDF 文件记录。
        source_file: 当前可编辑来源候选文件记录。
        pdf_document: PDF 的标准化文档记录；解析失败时为 None。
        source_document: 来源候选的标准化文档记录；解析失败时为 None。

    Returns:
        ``(匹配分数, 可解释匹配信号)``，分数范围为零到一。
    """
    pdf_preview = pdf_document["content_preview"] if pdf_document else ""
    source_preview = source_document["content_preview"] if source_document else ""
    content_score = calculate_text_similarity(
        pdf_preview,
        source_preview,
        max_characters=MAX_MATCH_PREVIEW_CHARACTERS,
    )
    key_score = calculate_key_field_similarity(
        pdf_document["key_fields"] if pdf_document else {},
        source_document["key_fields"] if source_document else {},
    )
    shared_topic = (
        pdf_file["normalized_stem"] == source_file["normalized_stem"]
        or filenames_share_topic(
            pdf_file["normalized_stem"],
            source_file["normalized_stem"],
        )
    )
    time_plausible = _is_source_time_plausible(
        source_file["modified_at"],
        pdf_file["modified_at"],
    )
    score = (
        0.65 * content_score
        + 0.20 * key_score
        + 0.10 * float(shared_topic)
        + 0.05 * float(time_plausible)
    )
    signals = [
        f"内容预览相似度 {content_score:.2f}",
        f"关键字段相似度 {key_score:.2f}",
    ]
    if shared_topic:
        signals.append("规范化文件名主题一致")
    if time_plausible:
        signals.append("可编辑版本修改时间不晚于 PDF")
    if (
        pdf_document
        and source_document
        and pdf_document["normalized_digest"]
        and pdf_document["normalized_digest"] == source_document["normalized_digest"]
    ):
        score = max(score, 0.98)
        signals.append("标准化内容摘要一致")
    return round(min(1.0, max(0.0, score)), 4), signals


def match_pdf_to_source_version(
    job: PdfMatchJob,
    files: Iterable[FileRecord],
    documents: Iterable[DocumentRecord],
    *,
    threshold: float = DEFAULT_PDF_MATCH_THRESHOLD,
) -> PdfExportRecord:
    """在任务限定的同组候选中匹配 PDF 的可编辑来源版本。

    该函数是确定性的纯匹配服务，只使用调用方传入的状态记录，不读取
    ``content_ref``、不访问原始文件、不调用网络，也不修改任何数据。只有最高
    分达到阈值且没有近似并列时才返回来源文件，证据不足时保留未匹配结果。

    Args:
        job: 包含 PDF、版本组和允许来源候选 ID 的匹配任务。
        files: 当前治理运行的文件记录。
        documents: 当前治理运行的标准化文档记录。
        threshold: 判定来源匹配成功所需的最低分数。

    Returns:
        可由 ``merge_by_id`` 合并的 ``PdfExportRecord``。

    Raises:
        ValueError: 阈值、ID、文件类型或任务引用不合法时抛出。
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold 必须位于 0.0 到 1.0 之间")
    file_list = [FileRecord(**dict(item)) for item in files]
    document_list = [DocumentRecord(**dict(item)) for item in documents]
    _ensure_unique_ids(file_list, label="文件记录")
    _ensure_unique_ids(document_list, label="文档记录")
    file_by_id = {item["id"]: item for item in file_list}
    document_by_file = {item["file_id"]: item for item in document_list}
    pdf_file = file_by_id.get(job["pdf_file_id"])
    if pdf_file is None:
        raise ValueError("PDF 匹配任务引用未知 pdf_file_id")
    if pdf_file["extension"] != ".pdf":
        raise ValueError("PDF 匹配任务的 pdf_file_id 必须指向 PDF 文件")
    if len(set(job["source_candidate_ids"])) != len(job["source_candidate_ids"]):
        raise ValueError("PDF 匹配任务包含重复来源候选 ID")

    ranked_candidates: list[tuple[float, str, list[str]]] = []
    for candidate_id in job["source_candidate_ids"]:
        candidate = file_by_id.get(candidate_id)
        if candidate is None:
            raise ValueError(f"PDF 匹配任务引用未知来源候选：{candidate_id}")
        if candidate["extension"] not in EDITABLE_EXTENSIONS:
            raise ValueError(f"PDF 来源候选不是支持的可编辑格式：{candidate_id}")
        score, signals = _score_pdf_source_candidate(
            pdf_file,
            candidate,
            document_by_file.get(pdf_file["id"]),
            document_by_file.get(candidate["id"]),
        )
        ranked_candidates.append((score, candidate_id, signals))

    ranked_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    source_file_id: str | None = None
    match_score = 0.0
    confidence = 0.0
    matched_signals: list[str] = ["当前版本组没有可编辑来源候选"]
    if ranked_candidates:
        match_score, best_candidate_id, best_signals = ranked_candidates[0]
        margin = (
            match_score - ranked_candidates[1][0]
            if len(ranked_candidates) > 1
            else 1.0
        )
        matched_signals = [
            f"最佳候选：{file_by_id[best_candidate_id]['file_name']}",
            *best_signals,
        ]
        if match_score < threshold:
            matched_signals.append(
                f"最高分 {match_score:.2f} 低于匹配阈值 {threshold:.2f}"
            )
            confidence = match_score
        elif len(ranked_candidates) > 1 and margin < MIN_PDF_MATCH_MARGIN:
            matched_signals.append(f"前两名候选分差仅为 {margin:.2f}")
            confidence = round(0.6 * match_score, 4)
        else:
            source_file_id = best_candidate_id
            confidence = round(
                min(1.0, 0.8 * match_score + 0.2 * min(margin / 0.20, 1.0)),
                4,
            )

    return PdfExportRecord(
        id=_stable_record_id("pdf-export", job["group_id"], job["pdf_file_id"]),
        group_id=job["group_id"],
        pdf_file_id=job["pdf_file_id"],
        source_file_id=source_file_id,
        match_score=round(match_score, 4),
        matched_signals=matched_signals,
        confidence=confidence,
    )


def _select_unique_canonical_file(
    candidates: Iterable[FileRecord],
    file_by_id: dict[str, FileRecord],
) -> FileRecord | None:
    """把完全重复候选折叠后选择唯一规范文件。

    Args:
        candidates: 具有相同匹配信号的文件候选。
        file_by_id: 文件 ID 到文件记录的完整映射。

    Returns:
        只有一个规范文件时返回该文件；零个或多个规范文件时返回 None。
    """
    canonical_ids = {
        candidate["duplicate_of"] or candidate["id"] for candidate in candidates
    }
    if len(canonical_ids) != 1:
        return None
    return file_by_id.get(next(iter(canonical_ids)))


def match_delivery_log_entry(
    entry: DeliveryLogEntry,
    files: Iterable[FileRecord],
    documents: Iterable[DocumentRecord],
    version_groups: Iterable[VersionGroupRecord],
) -> DeliveryRecord:
    """按强弱顺序把一条本地发送记录匹配到唯一文件版本。

    匹配依次使用 SHA-256、标准化内容摘要、完整文件名和规范化文件名主体。
    任一级出现多个非重复候选时不会猜测，而是保留 ``unmatched`` 结果。该函数
    不读取文件、不调用网络，也不会依据收件人名称推断业务关系。

    Args:
        entry: 已由本地日志工具校验的发送记录。
        files: 当前治理运行的文件记录。
        documents: 当前治理运行的标准化文档记录。
        version_groups: 当前治理运行识别出的版本组。

    Returns:
        与唯一文件匹配或明确标记为未匹配的 ``DeliveryRecord``。

    Raises:
        ValueError: 输入记录 ID 重复或版本组引用未知文件时抛出。
    """
    file_list = [FileRecord(**dict(item)) for item in files]
    document_list = [DocumentRecord(**dict(item)) for item in documents]
    group_list = [VersionGroupRecord(**dict(item)) for item in version_groups]
    _ensure_unique_ids(file_list, label="文件记录")
    _ensure_unique_ids(document_list, label="文档记录")
    _ensure_unique_ids(group_list, label="版本组")
    file_by_id = {item["id"]: item for item in file_list}
    group_by_file: dict[str, str] = {}
    for group in group_list:
        for file_id in group["file_ids"]:
            if file_id not in file_by_id:
                raise ValueError(f"版本组引用未知文件：{file_id}")
            if file_id in group_by_file:
                raise ValueError(f"文件同时属于多个版本组：{file_id}")
            group_by_file[file_id] = group["id"]

    matched_file: FileRecord | None = None
    match_method = "unmatched"
    confidence = 0.0
    if entry["attachment_sha256"]:
        matched_file = _select_unique_canonical_file(
            (
                item
                for item in file_list
                if item["sha256"].casefold() == entry["attachment_sha256"]
            ),
            file_by_id,
        )
        if matched_file:
            match_method = "sha256"
            confidence = 1.0

    if matched_file is None and entry["normalized_digest"]:
        document_by_file = {item["file_id"]: item for item in document_list}
        digest_candidates = []
        for file_record in file_list:
            canonical_id = file_record["duplicate_of"] or file_record["id"]
            document = document_by_file.get(canonical_id)
            if (
                document
                and document["normalized_digest"].casefold()
                == entry["normalized_digest"]
            ):
                digest_candidates.append(file_record)
        matched_file = _select_unique_canonical_file(digest_candidates, file_by_id)
        if matched_file:
            match_method = "normalized_digest"
            confidence = 0.95

    if matched_file is None:
        attachment_name = Path(entry["attachment_name"]).name.casefold()
        matched_file = _select_unique_canonical_file(
            (item for item in file_list if item["file_name"].casefold() == attachment_name),
            file_by_id,
        )
        if matched_file:
            match_method = "file_name"
            confidence = 0.85

    if matched_file is None:
        attachment_stem = normalize_filename_stem(entry["attachment_name"])
        matched_file = _select_unique_canonical_file(
            (
                item
                for item in file_list
                if item["normalized_stem"] == attachment_stem
            ),
            file_by_id,
        )
        if matched_file:
            match_method = "file_name"
            confidence = 0.70

    group_id = group_by_file.get(matched_file["id"]) if matched_file else None
    if matched_file is not None and group_id is None:
        raise ValueError(f"匹配文件未归入任何版本组：{matched_file['id']}")
    return DeliveryRecord(
        id=_stable_record_id("delivery", entry["id"]),
        group_id=group_id,
        file_id=matched_file["id"] if matched_file else None,
        evidence_source="local_log",
        sent_at=entry["sent_at"],
        recipient_label=entry["recipient_label"],
        evidence_ref=entry["evidence_ref"],
        match_method=match_method,
        customer_confirmed=entry["customer_confirmed"],
        confidence=confidence,
    )


def match_delivery_log_entries(
    entries: Iterable[DeliveryLogEntry],
    files: Iterable[FileRecord],
    documents: Iterable[DocumentRecord],
    version_groups: Iterable[VersionGroupRecord],
) -> list[DeliveryRecord]:
    """批量匹配本地发送记录并保持输入顺序。

    Args:
        entries: 已校验的本地发送记录。
        files: 当前治理运行的文件记录。
        documents: 当前治理运行的标准化文档记录。
        version_groups: 当前治理运行识别出的版本组。

    Returns:
        与输入记录一一对应的 ``DeliveryRecord`` 列表。

    Raises:
        ValueError: 发送记录 ID 重复或底层状态引用不一致时抛出。
    """
    entry_list = [DeliveryLogEntry(**dict(item)) for item in entries]
    _ensure_unique_ids(entry_list, label="本地发送记录")
    file_list = list(files)
    document_list = list(documents)
    group_list = list(version_groups)
    return [
        match_delivery_log_entry(entry, file_list, document_list, group_list)
        for entry in entry_list
    ]
