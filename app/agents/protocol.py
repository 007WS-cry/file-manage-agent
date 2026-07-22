from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Literal, cast

from app.state.models import (
    ContentSubagentInput,
    EvidenceSubagentInput,
    TeamMessage,
    TeamState,
    VersionSubagentInput,
)
from app.utils.runtime import utc_now_iso

"""本模块实现固定 Agent Team 的输入信封校验、消息创建和 Team Protocol 校验。"""

# Content Subagent 允许接收的最大内容预览字符数，禁止把完整正文塞入输入状态。
MAX_CONTENT_PREVIEW_CHARACTERS = 2_000

# 单个结构化摘要映射序列化后的最大字符数。
MAX_STRUCTURED_SUMMARY_CHARACTERS = 8_000

# Evidence Subagent 单项证据摘要允许的最大字符数。
MAX_EVIDENCE_SUMMARY_CHARACTERS = 4_000

# Team Protocol 消息摘要允许的最大字符数，与 Pydantic 输出上限保持一致。
MAX_TEAM_MESSAGE_SUMMARY_CHARACTERS = 4_000

# 单条错误说明允许的最大字符数，防止原始响应或正文进入 checkpoint。
MAX_TEAM_MESSAGE_ERROR_CHARACTERS = 1_000

# 单个产物引用允许的最大字符数。
MAX_ARTIFACT_REF_CHARACTERS = 2_048

# 单次 Subagent 输入或输出允许携带的最大产物引用数量。
MAX_ARTIFACT_REFS = 50

# 结构化摘要内单个字符串值的最大字符数。
MAX_STRUCTURED_STRING_CHARACTERS = 1_000

# 差异或排序信号列表中全部文本允许的最大字符数。
MAX_TEXT_LIST_TOTAL_CHARACTERS = 8_000

# 单次消息或输入中全部产物引用允许的最大字符数。
MAX_ARTIFACT_REFS_TOTAL_CHARACTERS = 20_000

# 输入映射中禁止出现的正文型字段名称。
FORBIDDEN_CONTENT_FIELD_NAMES = frozenset(
    {
        "content",
        "document_body",
        "document_content",
        "full_content",
        "full_text",
        "normalized_text",
        "raw_content",
        "raw_text",
        "完整正文",
        "正文",
    }
)

# Content Subagent 输入协议允许的固定字段。
CONTENT_INPUT_FIELDS = frozenset(
    {
        "task_id",
        "document_id",
        "content_preview",
        "structure_summary",
        "key_fields",
        "artifact_refs",
    }
)

# Version Subagent 输入协议允许的固定字段。
VERSION_INPUT_FIELDS = frozenset(
    {
        "task_id",
        "comparison_id",
        "file_labels",
        "structural_similarity",
        "content_similarity",
        "key_changes",
        "ordering_signals",
        "artifact_refs",
    }
)

# Evidence Subagent 输入协议允许的固定字段。
EVIDENCE_INPUT_FIELDS = frozenset(
    {
        "task_id",
        "group_id",
        "pdf_evidence_summary",
        "delivery_evidence_summary",
        "artifact_refs",
    }
)

# TeamMessage 运行时校验必须覆盖的全部协议字段。
TEAM_MESSAGE_FIELDS = frozenset(
    {
        "message_id",
        "task_id",
        "sender",
        "receiver",
        "message_type",
        "status",
        "summary",
        "artifact_refs",
        "error",
        "created_at",
    }
)

# Team Protocol 允许的消息类型。
TEAM_MESSAGE_TYPES = frozenset(
    {"assignment", "progress", "result", "question", "error"}
)

# Team Protocol 允许的消息状态。
TEAM_MESSAGE_STATUSES = frozenset({"created", "sent", "validated", "rejected"})


class TeamProtocolError(ValueError):
    """表示 Subagent 输入或 Team Message 违反固定团队协议。"""


def _reject_unknown_fields(
    payload: Mapping[str, object],
    *,
    allowed_fields: frozenset[str],
    payload_name: str,
) -> None:
    """拒绝输入信封中的未知字段，尤其是未声明的完整正文载荷。

    Args:
        payload: 等待检查的输入或消息映射。
        allowed_fields: 当前协议允许的字段集合。
        payload_name: 用于错误信息的协议对象名称。

    Raises:
        TeamProtocolError: 映射包含协议外字段时抛出。
    """
    unknown_fields = sorted(set(payload) - allowed_fields)
    if unknown_fields:
        raise TeamProtocolError(
            f"{payload_name} 包含协议外字段：{', '.join(unknown_fields)}"
        )


def _normalize_required_text(
    value: object,
    *,
    field_name: str,
    max_characters: int,
) -> str:
    """校验 Team Protocol 使用的必需短文本。

    Args:
        value: 等待校验的字段值。
        field_name: 用于错误信息的字段名称。
        max_characters: 允许的最大字符数。

    Returns:
        去除首尾空白后的非空文本。

    Raises:
        TeamProtocolError: 字段不是字符串、为空或超过长度上限时抛出。
    """
    if not isinstance(value, str):
        raise TeamProtocolError(f"{field_name} 必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise TeamProtocolError(f"{field_name} 不得为空")
    if len(normalized) > max_characters:
        raise TeamProtocolError(
            f"{field_name} 不得超过 {max_characters} 个字符"
        )
    return normalized


def _normalize_optional_error(value: object) -> str | None:
    """校验 Team Message 的可选脱敏错误说明。

    Args:
        value: ``None`` 或等待校验的错误文本。

    Returns:
        ``None`` 或去除首尾空白后的错误文本。

    Raises:
        TeamProtocolError: 错误值类型不合法、为空或超过上限时抛出。
    """
    if value is None:
        return None
    return _normalize_required_text(
        value,
        field_name="error",
        max_characters=MAX_TEAM_MESSAGE_ERROR_CHARACTERS,
    )


def _normalize_text_list(
    value: object,
    *,
    field_name: str,
    max_items: int,
    max_item_characters: int,
    exact_items: int | None = None,
    max_total_characters: int | None = None,
) -> list[str]:
    """校验并复制 Subagent 输入中的短文本列表。

    Args:
        value: 等待校验的列表值。
        field_name: 用于错误信息的字段名称。
        max_items: 允许的最大元素数量。
        max_item_characters: 单个元素允许的最大字符数。
        exact_items: 可选的固定元素数量。
        max_total_characters: 可选的列表全部文本字符数上限。

    Returns:
        去除首尾空白且不存在重复项的新列表。

    Raises:
        TeamProtocolError: 类型、数量、内容或重复性不符合协议时抛出。
    """
    if not isinstance(value, list):
        raise TeamProtocolError(f"{field_name} 必须是列表")
    if exact_items is not None and len(value) != exact_items:
        raise TeamProtocolError(f"{field_name} 必须包含 {exact_items} 项")
    if len(value) > max_items:
        raise TeamProtocolError(f"{field_name} 不得超过 {max_items} 项")

    normalized: list[str] = []
    for index, item in enumerate(value):
        text = _normalize_required_text(
            item,
            field_name=f"{field_name}[{index}]",
            max_characters=max_item_characters,
        )
        if text in normalized:
            raise TeamProtocolError(f"{field_name} 不得包含重复项：{text}")
        normalized.append(text)
    if max_total_characters is not None and sum(map(len, normalized)) > max_total_characters:
        raise TeamProtocolError(
            f"{field_name} 全部文本不得超过 {max_total_characters} 个字符"
        )
    return normalized


def _normalize_artifact_refs(value: object, *, field_name: str) -> list[str]:
    """校验并复制受控产物引用列表，不读取引用指向的文件。

    Args:
        value: 等待校验的产物引用列表。
        field_name: 用于错误信息的字段名称。

    Returns:
        顺序不变、非空且没有重复项的产物引用列表。

    Raises:
        TeamProtocolError: 引用类型、长度、数量或重复性不符合协议时抛出。
    """
    return _normalize_text_list(
        value,
        field_name=field_name,
        max_items=MAX_ARTIFACT_REFS,
        max_item_characters=MAX_ARTIFACT_REF_CHARACTERS,
        max_total_characters=MAX_ARTIFACT_REFS_TOTAL_CHARACTERS,
    )


def _validate_json_value(value: object, *, field_path: str) -> None:
    """递归验证结构化摘要只包含有界 JSON 值且没有正文型字段。

    Args:
        value: 当前递归层级的 JSON 候选值。
        field_path: 用于错误信息的字段路径。

    Raises:
        TeamProtocolError: 值不可序列化、数值非有限或字符串过长时抛出。
    """
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TeamProtocolError(f"{field_path} 不得包含非有限数值")
        return
    if isinstance(value, str):
        if len(value) > MAX_STRUCTURED_STRING_CHARACTERS:
            raise TeamProtocolError(
                f"{field_path} 的字符串不得超过 {MAX_STRUCTURED_STRING_CHARACTERS} 个字符"
            )
        return
    if isinstance(value, list):
        if len(value) > 100:
            raise TeamProtocolError(f"{field_path} 的列表不得超过 100 项")
        for index, item in enumerate(value):
            _validate_json_value(item, field_path=f"{field_path}[{index}]")
        return
    if isinstance(value, Mapping):
        if len(value) > 100:
            raise TeamProtocolError(f"{field_path} 的对象不得超过 100 个字段")
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise TeamProtocolError(f"{field_path} 的字段名必须是非空字符串")
            key = raw_key.strip()
            if key.casefold() in FORBIDDEN_CONTENT_FIELD_NAMES:
                raise TeamProtocolError(f"{field_path} 禁止包含正文型字段：{key}")
            _validate_json_value(item, field_path=f"{field_path}.{key}")
        return
    raise TeamProtocolError(f"{field_path} 只能包含 JSON 基础类型")


def _normalize_json_mapping(value: object, *, field_name: str) -> dict[str, Any]:
    """校验结构化摘要映射并通过 JSON 往返创建独立副本。

    Args:
        value: 等待校验的结构或关键字段映射。
        field_name: 用于错误信息的字段名称。

    Returns:
        与调用方解除引用关系的 JSON 映射。

    Raises:
        TeamProtocolError: 映射包含正文、超长内容或非 JSON 值时抛出。
    """
    if not isinstance(value, Mapping):
        raise TeamProtocolError(f"{field_name} 必须是对象")
    _validate_json_value(value, field_path=field_name)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(serialized) > MAX_STRUCTURED_SUMMARY_CHARACTERS:
        raise TeamProtocolError(
            f"{field_name} 序列化后不得超过 {MAX_STRUCTURED_SUMMARY_CHARACTERS} 个字符"
        )
    return cast(dict[str, Any], json.loads(serialized))


def _normalize_probability(value: object, *, field_name: str) -> float:
    """校验版本相似度为零到一之间的有限数值。

    Args:
        value: 等待校验的相似度。
        field_name: 用于错误信息的字段名称。

    Returns:
        浮点形式的合法相似度。

    Raises:
        TeamProtocolError: 值不是有限数值或超出零到一范围时抛出。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TeamProtocolError(f"{field_name} 必须是数值")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0 <= normalized <= 1:
        raise TeamProtocolError(f"{field_name} 必须位于 0 到 1 之间")
    return normalized


def validate_content_subagent_input(
    payload: Mapping[str, object],
) -> ContentSubagentInput:
    """校验 Content Subagent 只收到短预览、结构摘要和产物引用。

    Args:
        payload: 协调 Agent 生成的内容分析输入信封。

    Returns:
        字段经过规范化且不包含完整正文的独立输入对象。

    Raises:
        TeamProtocolError: 输入缺字段、含未知正文型字段或超过安全上限时抛出。
    """
    if not isinstance(payload, Mapping):
        raise TeamProtocolError("Content Subagent 输入必须是对象")
    _reject_unknown_fields(
        payload,
        allowed_fields=CONTENT_INPUT_FIELDS,
        payload_name="Content Subagent 输入",
    )
    return ContentSubagentInput(
        task_id=_normalize_required_text(
            payload.get("task_id"), field_name="task_id", max_characters=256
        ),
        document_id=_normalize_required_text(
            payload.get("document_id"), field_name="document_id", max_characters=256
        ),
        content_preview=_normalize_required_text(
            payload.get("content_preview"),
            field_name="content_preview",
            max_characters=MAX_CONTENT_PREVIEW_CHARACTERS,
        ),
        structure_summary=_normalize_json_mapping(
            payload.get("structure_summary"), field_name="structure_summary"
        ),
        key_fields=_normalize_json_mapping(
            payload.get("key_fields"), field_name="key_fields"
        ),
        artifact_refs=_normalize_artifact_refs(
            payload.get("artifact_refs"), field_name="artifact_refs"
        ),
    )


def validate_version_subagent_input(
    payload: Mapping[str, object],
) -> VersionSubagentInput:
    """校验 Version Subagent 只收到确定性差异摘要和产物引用。

    Args:
        payload: 协调 Agent 生成的版本比较输入信封。

    Returns:
        已规范化的文件对差异输入对象。

    Raises:
        TeamProtocolError: 输入包含未知字段、正文或非法相似度时抛出。
    """
    if not isinstance(payload, Mapping):
        raise TeamProtocolError("Version Subagent 输入必须是对象")
    _reject_unknown_fields(
        payload,
        allowed_fields=VERSION_INPUT_FIELDS,
        payload_name="Version Subagent 输入",
    )
    return VersionSubagentInput(
        task_id=_normalize_required_text(
            payload.get("task_id"), field_name="task_id", max_characters=256
        ),
        comparison_id=_normalize_required_text(
            payload.get("comparison_id"),
            field_name="comparison_id",
            max_characters=256,
        ),
        file_labels=_normalize_text_list(
            payload.get("file_labels"),
            field_name="file_labels",
            max_items=2,
            max_item_characters=256,
            exact_items=2,
        ),
        structural_similarity=_normalize_probability(
            payload.get("structural_similarity"),
            field_name="structural_similarity",
        ),
        content_similarity=_normalize_probability(
            payload.get("content_similarity"), field_name="content_similarity"
        ),
        key_changes=_normalize_text_list(
            payload.get("key_changes"),
            field_name="key_changes",
            max_items=50,
            max_item_characters=MAX_STRUCTURED_STRING_CHARACTERS,
            max_total_characters=MAX_TEXT_LIST_TOTAL_CHARACTERS,
        ),
        ordering_signals=_normalize_text_list(
            payload.get("ordering_signals"),
            field_name="ordering_signals",
            max_items=50,
            max_item_characters=MAX_STRUCTURED_STRING_CHARACTERS,
            max_total_characters=MAX_TEXT_LIST_TOTAL_CHARACTERS,
        ),
        artifact_refs=_normalize_artifact_refs(
            payload.get("artifact_refs"), field_name="artifact_refs"
        ),
    )


def validate_evidence_subagent_input(
    payload: Mapping[str, object],
) -> EvidenceSubagentInput:
    """校验 Evidence Subagent 只收到 PDF、发送证据摘要和产物引用。

    Args:
        payload: 协调 Agent 生成的证据分析输入信封。

    Returns:
        已规范化且不包含 PDF 或业务文件正文的证据输入对象。

    Raises:
        TeamProtocolError: 输入包含未知字段、正文或超长摘要时抛出。
    """
    if not isinstance(payload, Mapping):
        raise TeamProtocolError("Evidence Subagent 输入必须是对象")
    _reject_unknown_fields(
        payload,
        allowed_fields=EVIDENCE_INPUT_FIELDS,
        payload_name="Evidence Subagent 输入",
    )
    return EvidenceSubagentInput(
        task_id=_normalize_required_text(
            payload.get("task_id"), field_name="task_id", max_characters=256
        ),
        group_id=_normalize_required_text(
            payload.get("group_id"), field_name="group_id", max_characters=256
        ),
        pdf_evidence_summary=_normalize_required_text(
            payload.get("pdf_evidence_summary"),
            field_name="pdf_evidence_summary",
            max_characters=MAX_EVIDENCE_SUMMARY_CHARACTERS,
        ),
        delivery_evidence_summary=_normalize_required_text(
            payload.get("delivery_evidence_summary"),
            field_name="delivery_evidence_summary",
            max_characters=MAX_EVIDENCE_SUMMARY_CHARACTERS,
        ),
        artifact_refs=_normalize_artifact_refs(
            payload.get("artifact_refs"), field_name="artifact_refs"
        ),
    )


def _normalize_iso_timestamp(value: object) -> str:
    """校验 Team Message 时间为带时区的 ISO 8601 字符串。

    Args:
        value: 等待校验的时间字符串。

    Returns:
        原始 ISO 8601 时间字符串的规范化副本。

    Raises:
        TeamProtocolError: 时间为空、格式非法或缺少时区时抛出。
    """
    normalized = _normalize_required_text(
        value, field_name="created_at", max_characters=64
    )
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TeamProtocolError("created_at 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TeamProtocolError("created_at 必须包含时区")
    return normalized


def validate_team_message(
    payload: Mapping[str, object],
    *,
    team: TeamState,
    allowed_artifact_refs: Iterable[str] | None = None,
) -> TeamMessage:
    """验证 Team Message 的字段、成员、错误语义和受控引用。

    Args:
        payload: 等待校验的 Team Message 映射。
        team: 当前运行的固定团队状态。
        allowed_artifact_refs: 可选的当前任务引用白名单；省略时只做格式校验。

    Returns:
        可安全写入 LangGraph 状态的独立 Team Message。

    Raises:
        TeamProtocolError: 字段、成员、消息语义、时间或产物引用不合法时抛出。
    """
    if not isinstance(payload, Mapping):
        raise TeamProtocolError("Team Message 必须是对象")
    _reject_unknown_fields(
        payload,
        allowed_fields=TEAM_MESSAGE_FIELDS,
        payload_name="Team Message",
    )

    message_id = _normalize_required_text(
        payload.get("message_id"), field_name="message_id", max_characters=256
    )
    task_id = _normalize_required_text(
        payload.get("task_id"), field_name="task_id", max_characters=256
    )
    sender = _normalize_required_text(
        payload.get("sender"), field_name="sender", max_characters=128
    )
    receiver = _normalize_required_text(
        payload.get("receiver"), field_name="receiver", max_characters=128
    )
    if sender == receiver:
        raise TeamProtocolError("Team Message 的 sender 和 receiver 不得相同")

    member_ids = {
        member.get("id")
        for member in team.get("members", [])
        if isinstance(member.get("id"), str) and member.get("id")
    }
    coordinator_id = team.get("coordinator_id")
    if not isinstance(coordinator_id, str) or coordinator_id not in member_ids:
        raise TeamProtocolError("TeamState 缺少合法 coordinator_id")
    if sender not in member_ids or receiver not in member_ids:
        raise TeamProtocolError("Team Message 的 sender 和 receiver 必须属于固定团队")

    message_type = payload.get("message_type")
    if not isinstance(message_type, str) or message_type not in TEAM_MESSAGE_TYPES:
        raise TeamProtocolError("message_type 不属于 Team Protocol")
    status = payload.get("status")
    if not isinstance(status, str) or status not in TEAM_MESSAGE_STATUSES:
        raise TeamProtocolError("status 不属于 Team Protocol")

    summary = _normalize_required_text(
        payload.get("summary"),
        field_name="summary",
        max_characters=MAX_TEAM_MESSAGE_SUMMARY_CHARACTERS,
    )
    artifact_refs = _normalize_artifact_refs(
        payload.get("artifact_refs"), field_name="artifact_refs"
    )
    if allowed_artifact_refs is not None:
        allowed_refs = {
            item.strip()
            for item in allowed_artifact_refs
            if isinstance(item, str) and item.strip()
        }
        unauthorized = [item for item in artifact_refs if item not in allowed_refs]
        if unauthorized:
            raise TeamProtocolError(
                f"Team Message 包含未授权产物引用：{', '.join(unauthorized)}"
            )

    error = _normalize_optional_error(payload.get("error"))
    if message_type == "assignment" and sender != coordinator_id:
        raise TeamProtocolError("assignment 消息必须由 coordinator 发送")
    if message_type in {"result", "error"} and receiver != coordinator_id:
        raise TeamProtocolError("result 和 error 消息必须返回 coordinator")
    if message_type == "error" or status == "rejected":
        if error is None:
            raise TeamProtocolError("error 或 rejected 消息必须包含错误说明")
    elif error is not None:
        raise TeamProtocolError("非 error/rejected 消息不得携带 error")

    return TeamMessage(
        message_id=message_id,
        task_id=task_id,
        sender=sender,
        receiver=receiver,
        message_type=cast(
            Literal["assignment", "progress", "result", "question", "error"],
            message_type,
        ),
        status=cast(
            Literal["created", "sent", "validated", "rejected"], status
        ),
        summary=summary,
        artifact_refs=artifact_refs,
        error=error,
        created_at=_normalize_iso_timestamp(payload.get("created_at")),
    )


def create_team_message(
    *,
    team: TeamState,
    task_id: str,
    sender: str,
    receiver: str,
    message_type: Literal["assignment", "progress", "result", "question", "error"],
    summary: str,
    artifact_refs: Iterable[str],
    error: str | None = None,
    status: Literal["created", "sent", "validated", "rejected"] = "validated",
    created_at: str | None = None,
) -> TeamMessage:
    """创建具有确定性 ID 且立即通过 Team Protocol 校验的消息。

    Args:
        team: 当前运行的固定团队状态。
        task_id: 消息所属的真实 Task ID。
        sender: 发送方固定 Agent ID。
        receiver: 接收方固定 Agent ID。
        message_type: assignment、progress、result、question 或 error。
        summary: 不包含完整正文的简短消息摘要。
        artifact_refs: 当前消息携带的受控产物引用。
        error: error 或 rejected 消息使用的脱敏错误说明。
        status: 消息写入图状态时的协议状态。
        created_at: 可选带时区时间；省略时使用当前 UTC 时间。

    Returns:
        可由 ``merge_by_message_id`` 合并的合法 Team Message。
    """
    refs = list(artifact_refs)
    normalized_time = created_at or utc_now_iso()
    identity_payload = json.dumps(
        {
            "task_id": task_id,
            "sender": sender,
            "receiver": receiver,
            "message_type": message_type,
            "summary": summary,
            "artifact_refs": refs,
            "error": error,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    message_id = "team-message-" + hashlib.sha256(identity_payload.encode()).hexdigest()
    return validate_team_message(
        {
            "message_id": message_id,
            "task_id": task_id,
            "sender": sender,
            "receiver": receiver,
            "message_type": message_type,
            "status": status,
            "summary": summary,
            "artifact_refs": refs,
            "error": error,
            "created_at": normalized_time,
        },
        team=team,
        allowed_artifact_refs=refs,
    )


def create_assignment_message(
    *,
    team: TeamState,
    task_id: str,
    receiver: str,
    summary: str,
    artifact_refs: Iterable[str],
) -> TeamMessage:
    """创建 coordinator 发给固定 Subagent 的已校验任务分配消息。

    Args:
        team: 当前运行的固定团队状态。
        task_id: 被分配任务的 ID。
        receiver: 负责该任务的固定 Subagent ID。
        summary: 只描述任务范围和输入类型的短摘要。
        artifact_refs: Subagent 可以引用但不会自动读取的产物引用。

    Returns:
        状态为 ``validated`` 的 assignment Team Message。
    """
    return create_team_message(
        team=team,
        task_id=task_id,
        sender=team["coordinator_id"],
        receiver=receiver,
        message_type="assignment",
        status="validated",
        summary=summary,
        artifact_refs=artifact_refs,
    )


def create_result_message(
    *,
    team: TeamState,
    task_id: str,
    sender: str,
    summary: str,
    artifact_refs: Iterable[str],
) -> TeamMessage:
    """创建固定 Subagent 返回 coordinator 的结构化成功消息。

    Args:
        team: 当前运行的固定团队状态。
        task_id: 已完成任务的 ID。
        sender: 返回结果的固定 Subagent ID。
        summary: 经过 Pydantic 校验的简短结果摘要。
        artifact_refs: 经过调用方白名单校验的产物引用。

    Returns:
        只携带摘要和受控引用的 result Team Message。
    """
    return create_team_message(
        team=team,
        task_id=task_id,
        sender=sender,
        receiver=team["coordinator_id"],
        message_type="result",
        status="validated",
        summary=summary,
        artifact_refs=artifact_refs,
    )


def create_error_message(
    *,
    team: TeamState,
    task_id: str,
    sender: str,
    summary: str,
    error: str,
    artifact_refs: Iterable[str] = (),
) -> TeamMessage:
    """创建固定 Subagent 返回 coordinator 的脱敏错误消息。

    Args:
        team: 当前运行的固定团队状态。
        task_id: 失败任务的 ID；输入损坏时可使用协议保留 ID。
        sender: 报告错误的固定 Subagent ID。
        summary: 不含原始输入或模型响应的失败摘要。
        error: 经过截断和脱敏的错误原因。
        artifact_refs: 与失败相关且已受控的产物引用。

    Returns:
        能被 Team Protocol 正常校验和合并的 error Team Message。
    """
    return create_team_message(
        team=team,
        task_id=task_id,
        sender=sender,
        receiver=team["coordinator_id"],
        message_type="error",
        status="validated",
        summary=summary,
        artifact_refs=artifact_refs,
        error=error,
    )
