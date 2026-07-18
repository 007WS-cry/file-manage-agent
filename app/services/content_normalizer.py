from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.state.models import DocumentRecord, FileRecord, RawExtractedContent
from app.storage.artifacts import (
    load_normalized_content_artifact,
    save_normalized_content_artifact,
)

"""本模块把不同解析器的输出标准化为稳定文本、关键字段和可引用内容产物。"""


# 用于保守识别常见年月日格式的日期候选正则表达式。
DATE_PATTERN = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?:[-/.年](?:0?[1-9]|1[0-2])"
    r"(?:[-/.月](?:0?[1-9]|[12]\d|3[01])日?)?)(?!\d)"
)
# 用于识别带币种符号或千位分隔符金额的候选正则表达式。
AMOUNT_PATTERN = re.compile(
    r"(?:[¥￥]|RMB\s*|CNY\s*)\d+(?:,\d{3})*(?:\.\d{1,2})?"
    r"|(?<![\d,])\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?(?![\d,])",
    re.IGNORECASE,
)
# 用于识别字母前缀加数字主体的文档编号候选正则表达式。
DOCUMENT_CODE_PATTERN = re.compile(
    r"(?<![A-Z0-9])[A-Z]{2,12}[-_/]\d{2,}(?:[-_/][A-Z0-9]+)*(?![A-Z0-9])",
    re.IGNORECASE,
)


def _unique_values(values: Iterable[str], limit: int) -> list[str]:
    """按首次出现顺序去重文本值，并限制返回数量。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _make_json_safe(value: Any) -> Any:
    """递归把解析器值转换为可稳定写入 JSON 的基础类型。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_make_json_safe(item) for item in value]
    return str(value)


def normalize_text(text: str) -> str:
    """把解析文本转换为适合哈希和版本比较的稳定形式。

    处理包括 Unicode NFKC 规范化、统一换行、移除无意义控制字符、压缩
    单元格内空白和连续空行。制表符会被保留，用于表达表格或 Excel 列边界。

    Args:
        text: 任意解析器提取的原始文本。

    Returns:
        可重复计算摘要和相似度的标准化文本。

    Raises:
        TypeError: 输入不是字符串时抛出。
    """
    if not isinstance(text, str):
        raise TypeError("normalize_text 只接受字符串")

    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"} or unicodedata.category(character) != "Cc"
    )

    result_lines: list[str] = []
    previous_blank = False
    for raw_line in normalized.split("\n"):
        cells = [re.sub(r"[ \f\v]+", " ", cell).strip() for cell in raw_line.split("\t")]
        line = "\t".join(cells).rstrip("\t")
        is_blank = not line
        if is_blank and previous_blank:
            continue
        result_lines.append(line)
        previous_blank = is_blank

    return "\n".join(result_lines).strip()


def extract_key_fields(
    normalized_text: str,
    existing_fields: dict[str, Any] | None = None,
    *,
    max_values_per_field: int = 50,
) -> dict[str, Any]:
    """从标准化文本中提取可复核的日期、金额和文档编号候选值。

    当前只使用保守正则生成“候选字段”，不推断客户身份、合同效力或业务含义。
    解析器已经提供的字段会被保留；自动提取值放入独立的复数列表字段中。

    Args:
        normalized_text: 已由 ``normalize_text`` 处理的文本。
        existing_fields: 解析器提供的现有关键字段。
        max_values_per_field: 每类候选字段允许保留的最大去重值数量。

    Returns:
        可写入 JSON 的关键字段字典。

    Raises:
        ValueError: 字段数量上限不大于零时抛出。
    """
    if max_values_per_field <= 0:
        raise ValueError("max_values_per_field 必须大于零")

    fields = dict(_make_json_safe(existing_fields or {}))
    fields["date_candidates"] = _unique_values(
        (match.group(0) for match in DATE_PATTERN.finditer(normalized_text)),
        max_values_per_field,
    )
    fields["amount_candidates"] = _unique_values(
        (match.group(0) for match in AMOUNT_PATTERN.finditer(normalized_text)),
        max_values_per_field,
    )
    fields["document_code_candidates"] = _unique_values(
        (match.group(0) for match in DOCUMENT_CODE_PATTERN.finditer(normalized_text)),
        max_values_per_field,
    )
    return fields


def summarize_structure(structure: dict[str, Any]) -> dict[str, Any]:
    """压缩解析器结构信息，保留版本比较和报告需要的计数与摘要。"""
    safe_structure = _make_json_safe(structure)
    document_type = safe_structure.get("document_type", "unknown")
    common_keys = {"document_type", "parser", "truncated"}
    type_keys = {
        "xlsx": {"sheet_count", "sheets", "visited_cells"},
        "docx": {"paragraph_count", "table_count", "heading_counts", "tables"},
        "pdf": {"page_count", "pages"},
    }
    selected_keys = common_keys | type_keys.get(str(document_type), set())
    return {
        key: safe_structure[key]
        for key in selected_keys
        if key in safe_structure
    }


def normalize_document_content(
    file_record: FileRecord,
    raw_content: RawExtractedContent,
    artifact_root: str | Path,
    *,
    input_root: str | Path | None = None,
    preview_characters: int = 500,
) -> DocumentRecord:
    """标准化解析结果，并原子写入输入目录之外的 JSON 内容产物。

    该函数只写入显式提供的 ``artifact_root``，不会覆盖或创建原始业务文件
    的旁路副本。调用方提供 ``input_root`` 时，会拒绝把产物目录放在只读输入
    目录内部。状态只保存短预览和产物引用，完整正文不进入 LangGraph 状态。

    Args:
        file_record: 原始文件的只读元数据记录。
        raw_content: 文档解析器产生的统一原始内容。
        artifact_root: 允许写入标准化 JSON 的产物目录。
        input_root: 可选只读输入根目录，用于强制隔离产物。
        preview_characters: 状态中保留的最大预览字符数。

    Returns:
        指向标准化内容产物的 ``DocumentRecord``。

    Raises:
        ValueError: 参数无效或产物目录位于输入目录内部时抛出。
        OSError: 产物目录无法创建或 JSON 无法写入时抛出。
    """
    if preview_characters <= 0:
        raise ValueError("preview_characters 必须大于零")
    if not file_record.get("id"):
        raise ValueError("file_record 必须包含非空 id")

    normalized_text = normalize_text(raw_content.get("text", ""))
    structure_summary = summarize_structure(raw_content.get("structure", {}))
    key_fields = extract_key_fields(
        normalized_text,
        raw_content.get("key_fields", {}),
    )
    warnings = _unique_values(raw_content.get("warnings", []), 100)
    digest = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    document_id = hashlib.sha256(
        f"document:{file_record['id']}:{digest}".encode()
    ).hexdigest()
    payload = {
        "schema_version": "1.0",
        "document_id": document_id,
        "file_id": file_record["id"],
        "normalized_text": normalized_text,
        "structure": structure_summary,
        "key_fields": key_fields,
        "warnings": warnings,
    }

    artifact_path = save_normalized_content_artifact(
        artifact_root,
        document_id,
        payload,
        input_root=input_root,
    )

    parser_name = str(structure_summary.get("parser", "unknown"))
    return DocumentRecord(
        id=document_id,
        file_id=file_record["id"],
        parser_name=parser_name,
        content_ref=artifact_path,
        content_preview=normalized_text[:preview_characters],
        normalized_digest=digest,
        structure_summary=structure_summary,
        key_fields=key_fields,
        warnings=warnings,
    )


def load_normalized_content(
    content_ref: str | Path,
    *,
    max_artifact_size_bytes: int = 50 * 1024 * 1024,
) -> dict[str, Any]:
    """读取由本模块生成的本地标准化 JSON 产物并验证基础结构。

    Args:
        content_ref: ``DocumentRecord.content_ref`` 指向的 JSON 文件。
        max_artifact_size_bytes: 允许读取的产物文件大小上限。

    Returns:
        至少包含 ``normalized_text``、``structure`` 和 ``key_fields`` 的字典。

    Raises:
        ValueError: 路径不是 JSON 普通文件、文件超限或结构不合法时抛出。
        OSError: 文件无法读取时由操作系统抛出。
        json.JSONDecodeError: 产物内容不是有效 JSON 时抛出。
    """
    return load_normalized_content_artifact(
        content_ref,
        max_artifact_size_bytes=max_artifact_size_bytes,
    )
