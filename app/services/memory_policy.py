from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from app.state.models import (
    DecisionRecord,
    DeliveryRecord,
    MemoryItemState,
    MemoryState,
    PdfExportRecord,
    VersionGroupRecord,
)
from app.utils.runtime import utc_now_iso

"""本模块实施短期与长期 Memory 的最小化、脱敏、长度限制和召回应用策略。"""


# Memory 摘要允许的最大字符数，阻止长文档或完整模型 Prompt 被误存。
MAX_MEMORY_SUMMARY_LENGTH = 240

# 单条 Memory 最多保存的受控产物引用数量。
MAX_MEMORY_ARTIFACT_REFS = 16

# 历史人工选择对当前确定性推荐评分施加的有界增量。
RECALLED_CHOICE_SCORE_BOOST = 0.03

# 长期 Memory 允许持久化的结构化字段白名单，禁止任意业务数据透传。
ALLOWED_STRUCTURED_FIELDS = {
    "stage",
    "file_count",
    "group_count",
    "pdf_export_count",
    "delivery_count",
    "decision_count",
    "group_id",
    "selected_file_id",
    "evidence_type",
    "pdf_file_id",
    "source_file_id",
    "delivery_file_id",
    "match_method",
}

# 可疑凭据或认证头的保守匹配规则，用作固定模板之外的最后一道拒绝保护。
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"AIza[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
)

# Memory 中允许保存的运行、版本组、文件和记录 ID 字符集合。
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# Memory 条目 ID 和工作空间命名空间的固定哈希格式。
MEMORY_ITEM_ID_PATTERN = re.compile(r"^memory-[0-9a-f]{64}$")
MEMORY_NAMESPACE_PATTERN = re.compile(r"^workspace:[0-9a-f]{64}$")

# Memory 数据库约束允许的条目类型。
MemoryKind = Literal[
    "stage_summary",
    "confirmed_version_choice",
    "reliable_evidence_relation",
    "governance_preference",
]

# Memory 数据库约束允许的生命周期范围。
MemoryScope = Literal["short_term", "long_term"]


def derive_memory_namespace(root_directory: str | Path) -> str:
    """从规范化输入目录计算不可逆的工作空间 Memory 命名空间。

    Args:
        root_directory: 当前治理任务的输入根目录。

    Returns:
        不包含原始目录文本的 SHA-256 哈希命名空间。

    Raises:
        TypeError: 根目录不是字符串或 Path 时抛出。
        ValueError: 根目录字符串为空时抛出。
    """
    if not isinstance(root_directory, (str, Path)):
        raise TypeError("root_directory 必须是字符串或 Path")
    raw_path = str(root_directory).strip()
    if not raw_path:
        raise ValueError("root_directory 不得为空")
    normalized_path = str(Path(raw_path).expanduser().resolve()).casefold()
    digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()
    return f"workspace:{digest}"


def derive_configured_memory_namespace(namespace_seed: str) -> str:
    """从调用方配置的隔离种子计算与当前工作目录无关的哈希命名空间。

    Args:
        namespace_seed: 调用方用于隔离租户或业务空间的非空种子。

    Returns:
        不包含原始种子文本的 SHA-256 哈希命名空间。

    Raises:
        TypeError: 种子不是字符串时抛出。
        ValueError: 种子为空时抛出。
    """
    if not isinstance(namespace_seed, str):
        raise TypeError("namespace_seed 必须是字符串")
    normalized_seed = namespace_seed.strip()
    if not normalized_seed:
        raise ValueError("namespace_seed 不得为空")
    digest = hashlib.sha256(f"configured::{normalized_seed}".encode()).hexdigest()
    return f"workspace:{digest}"


def create_disabled_memory_state() -> MemoryState:
    """创建不会访问应用数据库的兼容性 Memory 状态。

    Returns:
        所有 Memory 缓冲区均为空且状态为 ``disabled`` 的新对象。
    """
    return MemoryState(
        enabled=False,
        namespace="",
        database_path=None,
        checkpoint_path=None,
        recall_limit=50,
        status="disabled",
        recalled_items=[],
        short_term_items=[],
        pending_long_term_items=[],
        persisted_item_ids=[],
        last_error=None,
    )


def copy_memory_item(item: Mapping[str, Any]) -> MemoryItemState:
    """深复制一个已经过安全策略校验的 Memory 条目。

    Args:
        item: 等待复制的 Memory 条目映射。

    Returns:
        结构化字典和引用列表均解除可变共享的 Memory 条目。
    """
    return MemoryItemState(
        id=str(item["id"]),
        namespace=str(item["namespace"]),
        scope=cast(MemoryScope, item["scope"]),
        kind=cast(MemoryKind, item["kind"]),
        summary=str(item["summary"]),
        structured_data=dict(item.get("structured_data", {})),
        artifact_refs=[str(ref) for ref in item.get("artifact_refs", [])],
        source_run_id=str(item["source_run_id"]),
        confirmed_by_human=bool(item["confirmed_by_human"]),
        confidence=float(item["confidence"]),
        created_at=str(item["created_at"]),
    )


def copy_memory_state(
    memory: Mapping[str, Any] | None,
) -> MemoryState:
    """复制 Memory 状态并为旧 checkpoint 补齐安全默认值。

    Args:
        memory: 当前状态中的可选 Memory 映射。

    Returns:
        与输入解除可变引用关系的完整 Memory 状态。
    """
    if memory is None:
        return create_disabled_memory_state()
    enabled = bool(memory.get("enabled", False))
    return MemoryState(
        enabled=enabled,
        namespace=str(memory.get("namespace", "")),
        database_path=(
            str(memory["database_path"])
            if memory.get("database_path") is not None
            else None
        ),
        checkpoint_path=(
            str(memory["checkpoint_path"])
            if memory.get("checkpoint_path") is not None
            else None
        ),
        recall_limit=int(memory.get("recall_limit", 50)),
        status=cast(
            Literal["disabled", "pending", "ready", "failed"],
            memory.get("status", "pending" if enabled else "disabled"),
        ),
        recalled_items=[
            copy_memory_item(item)
            for item in memory.get("recalled_items", [])
        ],
        short_term_items=[
            copy_memory_item(item)
            for item in memory.get("short_term_items", [])
        ],
        pending_long_term_items=[
            copy_memory_item(item)
            for item in memory.get("pending_long_term_items", [])
        ],
        persisted_item_ids=[
            str(item_id) for item_id in memory.get("persisted_item_ids", [])
        ],
        last_error=(
            str(memory["last_error"])
            if memory.get("last_error") is not None
            else None
        ),
    )


def _contains_secret(value: str) -> bool:
    """判断字符串是否命中常见凭据或认证头模式。

    Args:
        value: 等待检查的字符串。

    Returns:
        命中任一保守凭据规则时返回 True。
    """
    return any(pattern.search(value) is not None for pattern in SECRET_PATTERNS)


def _normalize_structured_value(value: Any, *, field_name: str) -> Any:
    """把白名单字段值限制为有界标量或短字符串列表。

    Args:
        value: 等待安全校验的结构化值。
        field_name: 当前结构化字段名称。

    Returns:
        可安全 JSON 序列化且不包含凭据的规范化值。

    Raises:
        TypeError: 值不是允许的标量或字符串列表时抛出。
        ValueError: 字符串过长、列表过大或包含可疑凭据时抛出。
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"structured_data.{field_name} 的浮点值必须位于 0 到 1")
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError(
                f"structured_data.{field_name} 必须是 1 到 128 字符的字符串"
            )
        if _contains_secret(normalized):
            raise ValueError(f"structured_data.{field_name} 疑似包含凭据")
        return normalized
    if isinstance(value, list):
        if len(value) > 16 or not all(isinstance(item, str) for item in value):
            raise TypeError(
                f"structured_data.{field_name} 只允许最多 16 个字符串"
            )
        return [
            _normalize_structured_value(item, field_name=field_name)
            for item in value
        ]
    raise TypeError(f"structured_data.{field_name} 包含不允许的值类型")


def validate_memory_item(item: Mapping[str, Any]) -> MemoryItemState:
    """严格校验待进入图状态或数据库的 Memory 条目。

    本函数只允许固定类型、短摘要、白名单结构化字段和受控引用。任何长正文、
    API Key、完整 Prompt 或未知业务字段都会在持久化前被拒绝。

    Args:
        item: 等待验证的 Memory 条目映射。

    Returns:
        已复制并规范化的安全 Memory 条目。

    Raises:
        TypeError: 字段类型不符合协议时抛出。
        ValueError: 字段为空、超限、越界、未知或疑似包含凭据时抛出。
    """
    required_fields = {
        "id",
        "namespace",
        "scope",
        "kind",
        "summary",
        "structured_data",
        "artifact_refs",
        "source_run_id",
        "confirmed_by_human",
        "confidence",
        "created_at",
    }
    unknown_fields = set(item) - required_fields
    missing_fields = required_fields - set(item)
    if unknown_fields:
        raise ValueError(f"Memory 条目包含未知字段：{', '.join(sorted(unknown_fields))}")
    if missing_fields:
        raise ValueError(f"Memory 条目缺少字段：{', '.join(sorted(missing_fields))}")

    scope = item["scope"]
    kind = item["kind"]
    if scope not in {"short_term", "long_term"}:
        raise ValueError("Memory scope 只能是 short_term 或 long_term")
    if kind not in {
        "stage_summary",
        "confirmed_version_choice",
        "reliable_evidence_relation",
        "governance_preference",
    }:
        raise ValueError("Memory kind 不在允许范围内")

    normalized_strings: dict[str, str] = {}
    for field_name in ("id", "namespace", "summary", "source_run_id", "created_at"):
        value = item[field_name]
        if not isinstance(value, str):
            raise TypeError(f"Memory {field_name} 必须是字符串")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"Memory {field_name} 不得为空")
        normalized_strings[field_name] = normalized
    if len(normalized_strings["id"]) > 128:
        raise ValueError("Memory id 不得超过 128 个字符")
    if len(normalized_strings["namespace"]) > 256:
        raise ValueError("Memory namespace 不得超过 256 个字符")
    if len(normalized_strings["source_run_id"]) > 64:
        raise ValueError("Memory source_run_id 不得超过 64 个字符")
    if len(normalized_strings["summary"]) > MAX_MEMORY_SUMMARY_LENGTH:
        raise ValueError("Memory summary 超过安全长度上限")
    if _contains_secret(normalized_strings["summary"]):
        raise ValueError("Memory summary 疑似包含凭据")
    if MEMORY_ITEM_ID_PATTERN.fullmatch(normalized_strings["id"]) is None:
        raise ValueError("Memory id 必须使用安全策略生成的固定哈希格式")
    if (
        MEMORY_NAMESPACE_PATTERN.fullmatch(normalized_strings["namespace"])
        is None
    ):
        raise ValueError("Memory namespace 必须使用固定工作空间哈希格式")
    if (
        SAFE_IDENTIFIER_PATTERN.fullmatch(
            normalized_strings["source_run_id"]
        )
        is None
    ):
        raise ValueError("Memory source_run_id 包含不允许的字符")
    try:
        datetime.fromisoformat(normalized_strings["created_at"])
    except ValueError as exc:
        raise ValueError("Memory created_at 必须是 ISO 8601 时间") from exc

    raw_structured_data = item["structured_data"]
    if not isinstance(raw_structured_data, Mapping):
        raise TypeError("Memory structured_data 必须是对象")
    unknown_data_fields = set(raw_structured_data) - ALLOWED_STRUCTURED_FIELDS
    if unknown_data_fields:
        raise ValueError(
            "Memory structured_data 包含未知字段："
            + ", ".join(sorted(unknown_data_fields))
        )
    structured_data = {
        str(field_name): _normalize_structured_value(
            value,
            field_name=str(field_name),
        )
        for field_name, value in raw_structured_data.items()
    }

    raw_refs = item["artifact_refs"]
    if not isinstance(raw_refs, list):
        raise TypeError("Memory artifact_refs 必须是字符串列表")
    if len(raw_refs) > MAX_MEMORY_ARTIFACT_REFS:
        raise ValueError("Memory artifact_refs 超过数量上限")
    artifact_refs = []
    for ref in raw_refs:
        if not isinstance(ref, str):
            raise TypeError("Memory artifact_refs 的元素必须是字符串")
        normalized_ref = ref.strip()
        if not normalized_ref or len(normalized_ref) > 256:
            raise ValueError("Memory artifact_ref 必须是 1 到 256 字符的字符串")
        if _contains_secret(normalized_ref):
            raise ValueError("Memory artifact_ref 疑似包含凭据")
        artifact_refs.append(normalized_ref)

    data_keys = set(structured_data)
    expected_summary: str
    if kind == "stage_summary":
        stage = structured_data.get("stage")
        if stage == "evidence":
            expected_keys = {"stage", "pdf_export_count", "delivery_count"}
            expected_summary = (
                f"Evidence 阶段完成：PDF 关系 {structured_data.get('pdf_export_count')} 条，"
                f"发送关系 {structured_data.get('delivery_count')} 条。"
            )
        elif stage == "recommendation":
            expected_keys = {"stage", "decision_count"}
            expected_summary = (
                f"Recommendation 阶段完成：生成 "
                f"{structured_data.get('decision_count')} 条推荐。"
            )
        else:
            raise ValueError("stage_summary 只允许 evidence 或 recommendation")
        if data_keys != expected_keys:
            raise ValueError("stage_summary 的结构化字段不符合固定协议")
        for field_name in expected_keys - {"stage"}:
            value = structured_data[field_name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"stage_summary.{field_name} 必须是非负整数")
        if artifact_refs:
            raise ValueError("stage_summary 不允许保存产物引用")
    elif kind == "confirmed_version_choice":
        if data_keys != {"group_id", "selected_file_id"}:
            raise ValueError("confirmed_version_choice 的结构化字段不符合固定协议")
        expected_summary = "用户已明确确认该版本组的主版本。"
        if artifact_refs:
            raise ValueError("confirmed_version_choice 不允许保存产物引用")
    elif kind == "reliable_evidence_relation":
        evidence_type = structured_data.get("evidence_type")
        if evidence_type == "pdf_source":
            expected_keys = {
                "evidence_type",
                "group_id",
                "pdf_file_id",
                "source_file_id",
            }
            expected_summary = "已验证 PDF 与可编辑源版本存在高置信度来源关系。"
            expected_ref_prefix = "pdf-export:"
        elif evidence_type == "customer_confirmed_delivery":
            expected_keys = {
                "evidence_type",
                "group_id",
                "delivery_file_id",
                "match_method",
            }
            expected_summary = "已验证客户确认记录与文件版本存在高置信度关系。"
            expected_ref_prefix = "delivery:"
            if structured_data.get("match_method") not in {
                "sha256",
                "normalized_digest",
                "file_name",
            }:
                raise ValueError("可靠发送关系包含未知匹配方法")
        else:
            raise ValueError("可靠证据关系包含未知 evidence_type")
        if data_keys != expected_keys:
            raise ValueError("reliable_evidence_relation 的结构化字段不符合固定协议")
        if len(artifact_refs) != 1 or not artifact_refs[0].startswith(
            expected_ref_prefix
        ):
            raise ValueError("可靠证据关系必须包含一个对应类型的受控引用")
    else:
        if data_keys != {"stage"} or structured_data.get("stage") != "recommendation":
            raise ValueError("governance_preference 的结构化字段不符合固定协议")
        expected_summary = "用户已确认当前工作空间的治理偏好。"
        if artifact_refs:
            raise ValueError("governance_preference 不允许保存产物引用")

    for field_name, value in structured_data.items():
        if field_name.endswith("_id") and (
            not isinstance(value, str)
            or SAFE_IDENTIFIER_PATTERN.fullmatch(value) is None
        ):
            raise ValueError(f"Memory {field_name} 包含不允许的字符")
    for artifact_ref in artifact_refs:
        if SAFE_IDENTIFIER_PATTERN.fullmatch(artifact_ref) is None:
            raise ValueError("Memory artifact_ref 包含不允许的字符")
    if normalized_strings["summary"] != expected_summary:
        raise ValueError("Memory summary 必须由固定安全模板生成")

    confirmed_by_human = item["confirmed_by_human"]
    if not isinstance(confirmed_by_human, bool):
        raise TypeError("Memory confirmed_by_human 必须是布尔值")
    confidence = item["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise TypeError("Memory confidence 必须是数字")
    normalized_confidence = float(confidence)
    if not 0.0 <= normalized_confidence <= 1.0:
        raise ValueError("Memory confidence 必须位于 0.0 到 1.0")

    return MemoryItemState(
        id=normalized_strings["id"],
        namespace=normalized_strings["namespace"],
        scope=cast(MemoryScope, scope),
        kind=cast(MemoryKind, kind),
        summary=normalized_strings["summary"],
        structured_data=structured_data,
        artifact_refs=artifact_refs,
        source_run_id=normalized_strings["source_run_id"],
        confirmed_by_human=confirmed_by_human,
        confidence=normalized_confidence,
        created_at=normalized_strings["created_at"],
    )


def create_memory_item(
    *,
    namespace: str,
    scope: MemoryScope,
    kind: MemoryKind,
    summary: str,
    structured_data: Mapping[str, Any],
    artifact_refs: Sequence[str],
    source_run_id: str,
    confirmed_by_human: bool,
    confidence: float,
) -> MemoryItemState:
    """由固定模板字段创建稳定 ID 的安全 Memory 条目。

    Args:
        namespace: 当前工作空间的哈希命名空间。
        scope: 条目是短期还是长期 Memory。
        kind: 数据库约束允许的 Memory 类型。
        summary: 固定模板生成的有界结论摘要。
        structured_data: 只包含白名单 ID、计数和证据类型的映射。
        artifact_refs: 受控记录 ID 形成的引用列表。
        source_run_id: 产生条目的治理运行 ID。
        confirmed_by_human: 是否来自用户明确确认。
        confidence: 条目置信度。

    Returns:
        已完成严格安全校验的 Memory 条目。
    """
    id_payload = json.dumps(
        {
            "namespace": namespace,
            "scope": scope,
            "kind": kind,
            "source_run_id": source_run_id,
            "structured_data": dict(structured_data),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    item_id = f"memory-{hashlib.sha256(id_payload.encode('utf-8')).hexdigest()}"
    return validate_memory_item(
        {
            "id": item_id,
            "namespace": namespace,
            "scope": scope,
            "kind": kind,
            "summary": summary,
            "structured_data": dict(structured_data),
            "artifact_refs": list(artifact_refs),
            "source_run_id": source_run_id,
            "confirmed_by_human": confirmed_by_human,
            "confidence": confidence,
            "created_at": utc_now_iso(),
        }
    )


def capture_evidence_memory(
    memory: Mapping[str, Any] | None,
    *,
    source_run_id: str,
    pdf_exports: Sequence[PdfExportRecord],
    deliveries: Sequence[DeliveryRecord],
    confidence_threshold: float,
) -> MemoryState:
    """从证据结果创建短期摘要和高置信度长期关系，不复制原始证据文本。

    Args:
        memory: 当前治理 Memory 状态。
        source_run_id: 当前治理运行 ID。
        pdf_exports: PDF 与可编辑来源的匹配结果。
        deliveries: 发送记录与文件版本的匹配结果。
        confidence_threshold: 允许形成长期关系的最低置信度。

    Returns:
        追加安全短期摘要和待持久化长期事实后的 Memory 状态。
    """
    result = copy_memory_state(memory)
    if not result["enabled"]:
        return result

    short_item = create_memory_item(
        namespace=result["namespace"],
        scope="short_term",
        kind="stage_summary",
        summary=(
            f"Evidence 阶段完成：PDF 关系 {len(pdf_exports)} 条，"
            f"发送关系 {len(deliveries)} 条。"
        ),
        structured_data={
            "stage": "evidence",
            "pdf_export_count": len(pdf_exports),
            "delivery_count": len(deliveries),
        },
        artifact_refs=[],
        source_run_id=source_run_id,
        confirmed_by_human=False,
        confidence=1.0,
    )
    result["short_term_items"].append(short_item)

    for export in pdf_exports:
        source_file_id = export.get("source_file_id")
        confidence = float(export.get("confidence", 0.0))
        if source_file_id is None or confidence < confidence_threshold:
            continue
        result["pending_long_term_items"].append(
            create_memory_item(
                namespace=result["namespace"],
                scope="long_term",
                kind="reliable_evidence_relation",
                summary="已验证 PDF 与可编辑源版本存在高置信度来源关系。",
                structured_data={
                    "evidence_type": "pdf_source",
                    "group_id": export["group_id"],
                    "pdf_file_id": export["pdf_file_id"],
                    "source_file_id": source_file_id,
                },
                artifact_refs=[f"pdf-export:{export['id']}"],
                source_run_id=source_run_id,
                confirmed_by_human=False,
                confidence=confidence,
            )
        )

    for delivery in deliveries:
        group_id = delivery.get("group_id")
        file_id = delivery.get("file_id")
        confidence = float(delivery.get("confidence", 0.0))
        if (
            group_id is None
            or file_id is None
            or not delivery.get("customer_confirmed", False)
            or confidence < confidence_threshold
        ):
            continue
        result["pending_long_term_items"].append(
            create_memory_item(
                namespace=result["namespace"],
                scope="long_term",
                kind="reliable_evidence_relation",
                summary="已验证客户确认记录与文件版本存在高置信度关系。",
                structured_data={
                    "evidence_type": "customer_confirmed_delivery",
                    "group_id": group_id,
                    "delivery_file_id": file_id,
                    "match_method": delivery["match_method"],
                },
                artifact_refs=[f"delivery:{delivery['id']}"],
                source_run_id=source_run_id,
                confirmed_by_human=False,
                confidence=confidence,
            )
        )
    return result


def capture_recommendation_memory(
    memory: Mapping[str, Any] | None,
    *,
    source_run_id: str,
    decisions: Sequence[DecisionRecord],
) -> MemoryState:
    """把当前推荐阶段的计数结论保存为短期 Memory。

    Args:
        memory: 当前治理 Memory 状态。
        source_run_id: 当前治理运行 ID。
        decisions: 已完成校验的推荐记录。

    Returns:
        追加固定模板阶段摘要后的 Memory 状态。
    """
    result = copy_memory_state(memory)
    if not result["enabled"]:
        return result
    result["short_term_items"].append(
        create_memory_item(
            namespace=result["namespace"],
            scope="short_term",
            kind="stage_summary",
            summary=f"Recommendation 阶段完成：生成 {len(decisions)} 条推荐。",
            structured_data={
                "stage": "recommendation",
                "decision_count": len(decisions),
            },
            artifact_refs=[],
            source_run_id=source_run_id,
            confirmed_by_human=False,
            confidence=1.0,
        )
    )
    return result


def capture_human_choice_memory(
    memory: Mapping[str, Any] | None,
    *,
    source_run_id: str,
    version_groups: Sequence[VersionGroupRecord],
    selections: Mapping[str, str],
) -> MemoryState:
    """把有效人工主版本选择转换为长期 Memory，并忽略用户自由文本说明。

    Args:
        memory: 当前治理 Memory 状态。
        source_run_id: 当前治理运行 ID。
        version_groups: 用于校验选择成员关系的版本组。
        selections: 版本组 ID 到用户选择文件 ID 的映射。

    Returns:
        追加人工确认长期事实后的 Memory 状态。

    Raises:
        ValueError: 选择引用未知版本组或组外文件时抛出。
    """
    result = copy_memory_state(memory)
    if not result["enabled"]:
        return result
    group_by_id = {group["id"]: group for group in version_groups}
    for group_id, selected_file_id in selections.items():
        group = group_by_id.get(group_id)
        if group is None:
            raise ValueError(f"人工选择引用未知版本组：{group_id}")
        if selected_file_id not in group["file_ids"]:
            raise ValueError(f"人工选择引用组外文件：{selected_file_id}")
        result["pending_long_term_items"].append(
            create_memory_item(
                namespace=result["namespace"],
                scope="long_term",
                kind="confirmed_version_choice",
                summary="用户已明确确认该版本组的主版本。",
                structured_data={
                    "group_id": group_id,
                    "selected_file_id": selected_file_id,
                },
                artifact_refs=[],
                source_run_id=source_run_id,
                confirmed_by_human=True,
                confidence=1.0,
            )
        )
    return result


def apply_recalled_choices(
    decisions: Sequence[DecisionRecord],
    recalled_items: Sequence[MemoryItemState],
) -> list[DecisionRecord]:
    """用同组历史人工选择对当前候选施加有界加分，不直接替代当前决策。

    Args:
        decisions: 当前运行按文件事实得到的基础候选评分。
        recalled_items: 当前命名空间召回的长期 Memory。

    Returns:
        仅对仍存在的同组候选增加固定小分值后的推荐记录副本。
    """
    confirmed_by_group: dict[str, str] = {}
    for item in recalled_items:
        if (
            item["kind"] != "confirmed_version_choice"
            or not item["confirmed_by_human"]
        ):
            continue
        group_id = item["structured_data"].get("group_id")
        selected_file_id = item["structured_data"].get("selected_file_id")
        if isinstance(group_id, str) and isinstance(selected_file_id, str):
            confirmed_by_group.setdefault(group_id, selected_file_id)

    updated: list[DecisionRecord] = []
    for decision in decisions:
        item = DecisionRecord(
            id=decision["id"],
            group_id=decision["group_id"],
            candidate_scores=dict(decision["candidate_scores"]),
            recommended_file_id=decision["recommended_file_id"],
            reasons=list(decision["reasons"]),
            confidence=float(decision["confidence"]),
            needs_human_review=bool(decision["needs_human_review"]),
            selected_by=decision["selected_by"],
            preserve_file_ids=list(decision["preserve_file_ids"]),
        )
        selected_file_id = confirmed_by_group.get(item["group_id"])
        if selected_file_id in item["candidate_scores"]:
            old_score = item["candidate_scores"][selected_file_id]
            item["candidate_scores"][selected_file_id] = min(
                1.0,
                old_score + RECALLED_CHOICE_SCORE_BOOST,
            )
            item["reasons"].append("同一工作空间的历史人工确认提供了有界偏好信号。")
        updated.append(item)
    return updated


def select_persistable_long_term_items(
    memory: Mapping[str, Any] | None,
) -> list[MemoryItemState]:
    """筛选并复验尚未持久化的长期 Memory 条目。

    Args:
        memory: 当前治理 Memory 状态。

    Returns:
        已通过严格安全校验且尚未写入数据库的长期条目。
    """
    normalized_memory = copy_memory_state(memory)
    persisted_ids = set(normalized_memory["persisted_item_ids"])
    result = []
    for item in normalized_memory["pending_long_term_items"]:
        validated = validate_memory_item(item)
        if validated["scope"] == "long_term" and validated["id"] not in persisted_ids:
            result.append(validated)
    return result
