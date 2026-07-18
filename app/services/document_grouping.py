from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any

from app.services.content_normalizer import load_normalized_content
from app.state.models import DocumentRecord, FileRecord, VersionGroupRecord

"""本模块通过文件名、标准化内容和关键字段把相关文件划分为独立版本组。"""


# 用于从文件名末尾移除版本号、日期和最终版等弱标记的正则表达式集合。
VERSION_TOKEN_PATTERNS = (
    re.compile(r"(?i)(?:^|[\s_\-.])(v(?:er(?:sion)?)?\s*\d+(?:\.\d+)*)$"),
    re.compile(r"(?i)(?:^|[\s_\-.])(rev(?:ision)?\s*[a-z0-9]+)$"),
    re.compile(r"(?:^|[\s_\-.])(版本\s*\d+(?:\.\d+)*)$"),
    re.compile(
        r"(?i)(?:final(?:版)?\d*|最终(?:最终)*(?:版)?\d*|定稿|终稿|"
        r"确认稿|确认版|修改版|修订版|copy|副本|复制件)$"
    ),
    re.compile(r"(?:人力资源?|财务|行政|业务|法务|采购|销售|市场|管理层)?反馈$"),
    re.compile(r"(?:别再?改(?:了)?|别改)$"),
    re.compile(r"\d{1,2}(?:[._-]\d{1,2}){1,2}$"),
    re.compile(r"(?:^|[\s_\-.])((?:19|20)\d{2}[-_.]?(?:0[1-9]|1[0-2])[-_.]?(?:0[1-9]|[12]\d|3[01]))$"),
)
# 用于将中文文件名中表示同类交付物的规划、方案、安排统一为“计划”。
DOCUMENT_TOPIC_SYNONYM_PATTERN = re.compile(r"(?:规划|方案|安排)")
# 用于识别文件名中的明确年份，防止把两个不同年度的文档仅凭主题强制合组。
EXPLICIT_YEAR_PATTERN = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# 用于从分组主题开头移除年份、年度和常见部门归属等弱限定词。
TOPIC_PREFIX_PATTERN = re.compile(
    r"^(?:(?:19|20)\d{2}(?:年度)?|年度|人力资源部?|人力|财务部?|财务|"
    r"行政部?|业务部?|法务部?|采购部?|销售部?|市场部?)+"
)


def normalize_filename_stem(file_name: str) -> str:
    """移除文件名末尾的常见版本弱标记并生成稳定分组名称。

    该函数只处理名称，不读取文件内容。版本号、日期、final、定稿和副本等
    标记只作为分组弱信号；返回结果不能单独证明两个文件属于同一版本链。

    Args:
        file_name: 文件名或包含路径的文件名字符串。

    Returns:
        小写、空白规范化后的文件名主体；若全部被移除则返回原始主体规范值。
    """
    original_stem = Path(file_name).stem
    normalized = unicodedata.normalize("NFKC", original_stem).strip().casefold()
    normalized = re.sub(r"[\[\](){}（）【】]", " ", normalized)

    previous = None
    while normalized != previous:
        previous = normalized
        for pattern in VERSION_TOKEN_PATTERNS:
            normalized = pattern.sub("", normalized).strip(" _-.")

    normalized = DOCUMENT_TOPIC_SYNONYM_PATTERN.sub("计划", normalized)
    normalized = re.sub(r"[\s_\-.]+", " ", normalized).strip()
    if normalized:
        return normalized
    return re.sub(r"[\s_\-.]+", " ", original_stem.casefold()).strip()


def extract_filename_topic(normalized_stem: str) -> str:
    """从规范化文件名中提取用于跨格式分组的稳定业务主题。

    年份、年度和常见部门前缀只在主题比较阶段作为弱限定词移除，原始文件名和
    ``normalized_stem`` 仍会保留这些信息。该主题只能作为分组证据，不能单独
    证明两个文件存在版本先后关系。

    Args:
        normalized_stem: 已由 ``normalize_filename_stem`` 处理的文件名主体。

    Returns:
        移除弱限定词并压缩空白后的主题；无法提取时返回原规范化主体。
    """
    topic = TOPIC_PREFIX_PATTERN.sub("", normalized_stem).strip()
    topic = re.sub(r"^年度", "", topic).strip()
    topic = re.sub(r"[\s_\-.]+", " ", topic).strip()
    return topic or normalized_stem


def filenames_share_topic(left_stem: str, right_stem: str) -> bool:
    """判断两个规范化文件名是否具有无年度冲突的相同业务主题。

    Args:
        left_stem: 第一个规范化文件名主体。
        right_stem: 第二个规范化文件名主体。

    Returns:
        主题长度足够、主题完全一致且明确年份不冲突时返回 ``True``。
    """
    left_topic = extract_filename_topic(left_stem)
    right_topic = extract_filename_topic(right_stem)
    left_years = set(EXPLICIT_YEAR_PATTERN.findall(left_stem))
    right_years = set(EXPLICIT_YEAR_PATTERN.findall(right_stem))
    years_conflict = bool(left_years and right_years and left_years.isdisjoint(right_years))
    return (
        len(left_topic.replace(" ", "")) >= 4
        and left_topic == right_topic
        and not years_conflict
    )


def calculate_text_similarity(
    left_text: str,
    right_text: str,
    *,
    ngram_size: int = 3,
    max_characters: int = 50_000,
) -> float:
    """综合字符 n-gram 与顺序相似度比较两段标准化文本。

    字符 n-gram 适合中文和表格值重合，顺序相似度能够补充 XLSX、DOCX、PDF
    因换行和结构标记不同造成的差异。输入会被截断以限制资源消耗；该分数只
    表示内容接近程度，不表示版本先后或业务等价。

    Args:
        left_text: 第一段标准化文本。
        right_text: 第二段标准化文本。
        ngram_size: 字符片段长度，必须大于零。
        max_characters: 每段最多参与计算的字符数。

    Returns:
        ``0.0`` 到 ``1.0`` 之间的相似度。

    Raises:
        ValueError: n-gram 长度或字符上限不合法时抛出。
    """
    if ngram_size <= 0 or max_characters <= 0:
        raise ValueError("ngram_size 和 max_characters 必须大于零")

    left = left_text[:max_characters].casefold()
    right = right_text[:max_characters].casefold()
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    if min(len(left), len(right)) < ngram_size:
        return SequenceMatcher(None, left, right).ratio()

    left_ngrams = {
        left[index : index + ngram_size]
        for index in range(len(left) - ngram_size + 1)
    }
    right_ngrams = {
        right[index : index + ngram_size]
        for index in range(len(right) - ngram_size + 1)
    }
    union = left_ngrams | right_ngrams
    ngram_score = len(left_ngrams & right_ngrams) / len(union) if union else 1.0
    sequence_limit = min(max_characters, 20_000)
    sequence_score = SequenceMatcher(
        None,
        left[:sequence_limit],
        right[:sequence_limit],
    ).ratio()
    return min(1.0, 0.55 * ngram_score + 0.45 * sequence_score)


def calculate_key_field_similarity(
    left_fields: dict[str, Any],
    right_fields: dict[str, Any],
) -> float:
    """把关键字段展平后计算 Jaccard 相似度，不推断字段的业务语义。"""

    def flatten(fields: dict[str, Any]) -> set[str]:
        """把关键字段值递归转换为带字段名的可比较字符串集合。"""
        values: set[str] = set()
        for key, value in fields.items():
            items = value if isinstance(value, list) else [value]
            for item in items:
                if item not in (None, ""):
                    values.add(f"{key}:{str(item).strip().casefold()}")
        return values

    left_values = flatten(left_fields)
    right_values = flatten(right_fields)
    if not left_values and not right_values:
        return 0.0
    union = left_values | right_values
    return len(left_values & right_values) / len(union)


def score_document_pair(
    left_file: FileRecord,
    right_file: FileRecord,
    left_document: DocumentRecord | None,
    right_document: DocumentRecord | None,
    left_text: str,
    right_text: str,
) -> tuple[float, list[str]]:
    """综合文件名主题、内容和关键字段计算版本分组分数与证据。

    相同业务主题只有在内容或关键字段达到最低支持时才会触发跨格式加权，避免
    仅凭常见标题合并无关文档。明确年份冲突不会使用主题一致加权。
    """
    strict_name_score = SequenceMatcher(
        None,
        left_file["normalized_stem"],
        right_file["normalized_stem"],
    ).ratio()
    left_topic = extract_filename_topic(left_file["normalized_stem"])
    right_topic = extract_filename_topic(right_file["normalized_stem"])
    topic_score = SequenceMatcher(None, left_topic, right_topic).ratio()
    left_years = set(EXPLICIT_YEAR_PATTERN.findall(left_file["normalized_stem"]))
    right_years = set(EXPLICIT_YEAR_PATTERN.findall(right_file["normalized_stem"]))
    years_conflict = bool(
        left_years and right_years and left_years.isdisjoint(right_years)
    )
    shared_topic = filenames_share_topic(
        left_file["normalized_stem"],
        right_file["normalized_stem"],
    )
    name_score = max(
        strict_name_score,
        0.95 if shared_topic else (0.0 if years_conflict else 0.90 * topic_score),
    )
    content_score = calculate_text_similarity(left_text, right_text)
    key_score = calculate_key_field_similarity(
        left_document["key_fields"] if left_document else {},
        right_document["key_fields"] if right_document else {},
    )

    if left_file["sha256"] == right_file["sha256"]:
        return 1.0, ["SHA-256 完全一致"]

    score = 0.55 * name_score + 0.35 * content_score + 0.10 * key_score
    signals = [
        f"文件名相似度 {name_score:.2f}",
        f"内容相似度 {content_score:.2f}",
    ]
    if key_score > 0:
        signals.append(f"关键字段相似度 {key_score:.2f}")
    if years_conflict:
        signals.append("文件名明确年份冲突，不使用业务主题加权")

    if left_file["normalized_stem"] == right_file["normalized_stem"]:
        signals.append("规范化文件名主体一致")
    if shared_topic:
        signals.append(f"文件名业务主题一致：{left_topic}")
        same_format = left_file["extension"] == right_file["extension"]
        minimum_content_support = 0.18 if same_format else 0.08
        has_support = (
            content_score >= minimum_content_support
            or key_score >= 0.25
        )
        if has_support:
            support_score = max(content_score, key_score)
            score = max(score, min(0.94, 0.78 + 0.12 * support_score))
            signals.append("相同业务主题获得内容或关键字段支持")

    if (
        left_document
        and right_document
        and left_document["normalized_digest"] == right_document["normalized_digest"]
    ):
        score = max(score, 0.95)
        signals.append("标准化内容摘要一致")

    return min(1.0, score), signals


def group_related_documents(
    files: Iterable[FileRecord],
    documents: Iterable[DocumentRecord],
    *,
    similarity_threshold: float = 0.72,
) -> list[VersionGroupRecord]:
    """根据可解释相似度把文件划分为互不重叠的版本组。

    分组采用并查集连接达到阈值的文件对。完全重复文件会映射到规范文件内容；
    无法读取内容产物时仍可基于文件名分组，但不会伪造内容相似证据。该函数
    只读取标准化产物，不修改原始文件或产物。

    Args:
        files: 扫描得到的全部文件记录。
        documents: 成功标准化的文档记录。
        similarity_threshold: 文件对归入同组的最低综合相似度。

    Returns:
        按标签和组 ID 稳定排序的版本组记录。

    Raises:
        ValueError: 阈值不在 ``0.0`` 到 ``1.0`` 之间或文件 ID 重复时抛出。
    """
    if not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold 必须位于 0.0 到 1.0 之间")

    file_list = [FileRecord(**dict(item)) for item in files]
    if len({item["id"] for item in file_list}) != len(file_list):
        raise ValueError("文件记录中存在重复 id")
    if not file_list:
        return []

    document_by_file = {item["file_id"]: item for item in documents}
    file_by_id = {item["id"]: item for item in file_list}
    text_by_file: dict[str, str] = {}
    for file_record in file_list:
        canonical_id = file_record["duplicate_of"] or file_record["id"]
        document = document_by_file.get(canonical_id)
        if document is None:
            text_by_file[file_record["id"]] = ""
            continue
        try:
            payload = load_normalized_content(document["content_ref"])
            text_by_file[file_record["id"]] = str(payload["normalized_text"])
        except (OSError, ValueError):
            # 后续循环会改用状态中的短预览，避免单个损坏产物阻断整个目录。
            continue

    # 产物读取失败时使用预览作为受限降级，不让单个坏产物终止整个目录分组。
    for file_record in file_list:
        if file_record["id"] in text_by_file:
            continue
        canonical_id = file_record["duplicate_of"] or file_record["id"]
        document = document_by_file.get(canonical_id)
        text_by_file[file_record["id"]] = document["content_preview"] if document else ""

    parent = {item["id"]: item["id"] for item in file_list}

    def find(item_id: str) -> str:
        """返回并查集根节点，并执行路径压缩。"""
        while parent[item_id] != item_id:
            parent[item_id] = parent[parent[item_id]]
            item_id = parent[item_id]
        return item_id

    def union(left_id: str, right_id: str) -> None:
        """按稳定 ID 顺序合并两个并查集。"""
        left_root = find(left_id)
        right_root = find(right_id)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        parent[second] = first

    pair_evidence: dict[frozenset[str], tuple[float, list[str]]] = {}
    for left_file, right_file in combinations(file_list, 2):
        left_canonical = left_file["duplicate_of"] or left_file["id"]
        right_canonical = right_file["duplicate_of"] or right_file["id"]
        score, signals = score_document_pair(
            left_file,
            right_file,
            document_by_file.get(left_canonical),
            document_by_file.get(right_canonical),
            text_by_file.get(left_file["id"], ""),
            text_by_file.get(right_file["id"], ""),
        )
        pair_evidence[frozenset((left_file["id"], right_file["id"]))] = (
            score,
            signals,
        )
        if score >= similarity_threshold:
            union(left_file["id"], right_file["id"])

    grouped_ids: dict[str, list[str]] = {}
    for file_record in file_list:
        grouped_ids.setdefault(find(file_record["id"]), []).append(file_record["id"])

    results: list[VersionGroupRecord] = []
    for member_ids in grouped_ids.values():
        sorted_ids = sorted(member_ids)
        stems = [file_by_id[item_id]["normalized_stem"] for item_id in sorted_ids]
        label = Counter(stems).most_common(1)[0][0] or file_by_id[sorted_ids[0]]["file_name"]
        scores: list[float] = []
        signals: list[str] = []
        for left_id, right_id in combinations(sorted_ids, 2):
            pair_score, pair_signals = pair_evidence[frozenset((left_id, right_id))]
            if pair_score < similarity_threshold:
                continue
            scores.append(pair_score)
            for signal in pair_signals:
                rendered = (
                    f"{file_by_id[left_id]['file_name']} ↔ "
                    f"{file_by_id[right_id]['file_name']}: {signal}"
                )
                if rendered not in signals and len(signals) < 20:
                    signals.append(rendered)

        confidence = sum(scores) / len(scores) if scores else 1.0
        group_id = hashlib.sha256("\n".join(sorted_ids).encode("utf-8")).hexdigest()
        results.append(
            VersionGroupRecord(
                id=group_id,
                label=label,
                file_ids=sorted_ids,
                grouping_signals=signals or ["单文件独立版本组"],
                confidence=round(confidence, 4),
            )
        )

    return sorted(results, key=lambda item: (item["label"].casefold(), item["id"]))
