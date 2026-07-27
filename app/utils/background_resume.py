from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from app.state.models import BackgroundResumeState, PendingInterruptState

"""本模块校验并规范化后台 LangGraph 中断快照与幂等人工恢复请求。"""


# 后台 API 只允许公开并恢复这两种经过项目协议约束的中断。
SUPPORTED_INTERRUPT_KINDS = frozenset({"file_governance_review", "error_recovery"})

# 单个中断公开载荷允许持久化的最大 UTF-8 字节数。
MAX_INTERRUPT_PAYLOAD_BYTES = 128 * 1024

# 单个人工恢复值允许持久化的最大 UTF-8 字节数。
MAX_RESUME_VALUE_BYTES = 64 * 1024


def _canonical_json(value: Mapping[str, Any], *, field_name: str) -> str:
    """把协议对象序列化为稳定 JSON，用于大小限制和幂等摘要。

    Args:
        value: 只应包含 JSON 基础类型的协议对象。
        field_name: 校验失败时使用的安全字段名称。

    Returns:
        键顺序稳定、无多余空白的 UTF-8 JSON 文本。

    Raises:
        ValueError: 对象包含不可序列化值时抛出。
    """
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须只包含可 JSON 序列化的数据") from exc


def _validate_kind(value: object) -> str:
    """校验中断协议类型是否属于后台 API 白名单。

    Args:
        value: 中断载荷或恢复请求提供的类型值。

    Returns:
        已通过白名单校验的协议类型字符串。

    Raises:
        ValueError: 类型缺失或不受支持时抛出。
    """
    kind = str(value or "").strip()
    if kind not in SUPPORTED_INTERRUPT_KINDS:
        raise ValueError("中断 kind 不受后台恢复 API 支持")
    return kind


def serialize_pending_interrupt(
    interrupts: object,
    *,
    created_at: str,
) -> PendingInterruptState | None:
    """把 LangGraph ``__interrupt__`` 结果收敛为可持久化的最小中断快照。

    本函数不是 LLM Tool，不执行 checkpoint 读取或人工选择。为了避免调用方恢复
    错误 checkpoint，一项后台任务同一时刻只接受一个带稳定 ID 的受支持中断。

    Args:
        interrupts: ``graph.invoke`` 返回的 ``__interrupt__`` 值。
        created_at: Worker 观察到当前中断的 ISO 8601 时间。

    Returns:
        没有中断时返回 None；存在一个合法中断时返回最小持久化状态。

    Raises:
        ValueError: 中断数量、ID、载荷、协议类型或大小不符合约束时抛出。
    """
    if interrupts is None:
        return None
    if not isinstance(interrupts, Sequence) or isinstance(
        interrupts,
        (str, bytes, bytearray),
    ):
        raise ValueError("LangGraph 中断结果必须是序列")
    items = list(interrupts)
    if not items:
        return None
    if len(items) != 1:
        raise ValueError("后台任务同一时刻只能持久化一个 LangGraph 中断")
    item = items[0]
    interrupt_id = str(getattr(item, "id", "") or "").strip()
    payload_value = getattr(item, "value", item)
    if not interrupt_id:
        raise ValueError("LangGraph 中断缺少稳定 interrupt_id")
    if not isinstance(payload_value, Mapping):
        raise ValueError("LangGraph 中断载荷必须是对象")
    payload = dict(payload_value)
    kind = _validate_kind(payload.get("kind"))
    serialized = _canonical_json(payload, field_name="中断载荷")
    if len(serialized.encode("utf-8")) > MAX_INTERRUPT_PAYLOAD_BYTES:
        raise ValueError("LangGraph 中断载荷超过后台持久化上限")
    return PendingInterruptState(
        interrupt_id=interrupt_id,
        kind=cast(Any, kind),
        payload=payload,
        created_at=created_at,
    )


def build_background_resume_state(
    *,
    request_id: str,
    interrupt_id: str,
    kind: str,
    value: Mapping[str, Any],
    submitted_at: str,
) -> BackgroundResumeState:
    """校验 API 人工输入并构造可幂等入队的后台恢复状态。

    本函数不是 LLM Tool，不应用人工选择，也不调用 LangGraph。它只负责稳定
    JSON 摘要、大小限制和协议元数据，实际恢复由 Worker 使用 checkpoint 完成。

    Args:
        request_id: 调用方提供的幂等键。
        interrupt_id: 调用方明确希望消费的当前中断 ID。
        kind: 恢复值遵循的中断协议类型。
        value: 传给 ``Command(resume=...)`` 的 JSON 对象。
        submitted_at: API 首次接受请求的 ISO 8601 时间。

    Returns:
        状态为 pending、包含值摘要的后台恢复状态。

    Raises:
        ValueError: 标识、类型、值或大小不符合协议时抛出。
    """
    normalized_request_id = str(request_id or "").strip()
    normalized_interrupt_id = str(interrupt_id or "").strip()
    if not normalized_request_id:
        raise ValueError("request_id 不能为空")
    if not normalized_interrupt_id:
        raise ValueError("interrupt_id 不能为空")
    normalized_kind = _validate_kind(kind)
    serialized = _canonical_json(value, field_name="恢复值")
    if len(serialized.encode("utf-8")) > MAX_RESUME_VALUE_BYTES:
        raise ValueError("恢复值超过后台持久化上限")
    return BackgroundResumeState(
        request_id=normalized_request_id,
        interrupt_id=normalized_interrupt_id,
        kind=cast(Any, normalized_kind),
        value=dict(value),
        value_digest=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        status="pending",
        submitted_at=submitted_at,
        applied_at=None,
    )


def is_same_resume_request(
    existing: Mapping[str, Any] | None,
    candidate: BackgroundResumeState,
) -> bool:
    """判断已持久化恢复状态是否与候选请求具有完全相同的幂等身份。

    Args:
        existing: 数据库中最近一次 pending 或 applied 恢复状态。
        candidate: 本次 API 请求规范化后的 pending 恢复状态。

    Returns:
        幂等键、中断、协议类型和值摘要全部一致时返回 True。
    """
    if existing is None:
        return False
    return all(
        existing.get(field_name) == candidate.get(field_name)
        for field_name in ("request_id", "interrupt_id", "kind", "value_digest")
    )


def validate_resume_value_against_interrupt(
    pending_interrupt: Mapping[str, Any],
    resume_state: BackgroundResumeState,
) -> None:
    """按照当前中断公开载荷校验恢复值，阻止无效人工输入进入 Worker 队列。

    本函数不是 LLM Tool，不修改 checkpoint。主版本审核必须恰好覆盖所有待审核组
    且选择组内文件；错误恢复动作必须来自当前中断的白名单，并遵守路径字段约束。

    Args:
        pending_interrupt: 数据库中当前 waiting_human 中断快照。
        resume_state: 已完成通用 JSON、大小和摘要校验的候选恢复状态。

    Raises:
        ValueError: 恢复值字段、组选择、动作或可选说明不符合当前中断协议时抛出。
    """
    value = resume_state.get("value")
    payload = pending_interrupt.get("payload")
    if not isinstance(value, Mapping) or not isinstance(payload, Mapping):
        raise ValueError("当前中断或恢复值缺少可校验的协议对象")
    kind = resume_state["kind"]
    if kind == "file_governance_review":
        unknown_fields = set(value) - {"selections", "review_note"}
        if unknown_fields:
            raise ValueError("主版本审核恢复值包含未知字段")
        selections = value.get("selections")
        groups = payload.get("groups")
        if (
            not isinstance(selections, Mapping)
            or not isinstance(groups, Sequence)
            or isinstance(groups, (str, bytes, bytearray))
        ):
            raise ValueError("主版本审核恢复值必须包含 selections 对象")
        candidates_by_group: dict[str, set[str]] = {}
        for group in groups:
            if not isinstance(group, Mapping):
                raise ValueError("当前主版本审核中断包含损坏的版本组")
            group_id = group.get("group_id")
            candidates = group.get("candidates")
            if (
                not isinstance(group_id, str)
                or not isinstance(candidates, Sequence)
                or isinstance(candidates, (str, bytes, bytearray))
            ):
                raise ValueError("当前主版本审核中断缺少版本组候选")
            candidates_by_group[group_id] = {
                str(candidate.get("file_id"))
                for candidate in candidates
                if isinstance(candidate, Mapping) and isinstance(candidate.get("file_id"), str)
            }
        if set(selections) != set(candidates_by_group):
            raise ValueError("selections 必须恰好覆盖当前全部待审核版本组")
        for group_id, file_id in selections.items():
            if not isinstance(file_id, str) or file_id not in candidates_by_group[group_id]:
                raise ValueError(f"版本组 {group_id} 必须选择当前组内文件")
        review_note = value.get("review_note")
        if review_note is not None and (
            not isinstance(review_note, str) or len(review_note.strip()) > 500
        ):
            raise ValueError("review_note 必须是最多 500 字符的字符串或 null")
        return

    unknown_fields = set(value) - {"action", "replacement_path", "note"}
    if unknown_fields:
        raise ValueError("错误恢复值包含未知字段")
    allowed_actions = payload.get("allowed_actions")
    action = value.get("action")
    if (
        not isinstance(allowed_actions, Sequence)
        or isinstance(allowed_actions, (str, bytes, bytearray))
        or action not in allowed_actions
    ):
        raise ValueError("错误恢复动作不在当前允许列表中")
    replacement_path = value.get("replacement_path")
    if action == "provide_path":
        if not isinstance(replacement_path, str) or not replacement_path.strip():
            raise ValueError("provide_path 必须提供非空 replacement_path")
    elif replacement_path is not None:
        raise ValueError("非 provide_path 动作不得携带 replacement_path")
    note = value.get("note")
    if note is not None and (not isinstance(note, str) or len(note.strip()) > 500):
        raise ValueError("note 必须是最多 500 字符的字符串或 null")
