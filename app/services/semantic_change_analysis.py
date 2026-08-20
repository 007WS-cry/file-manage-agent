from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from difflib import SequenceMatcher
from typing import Any, cast

from app.state.models import (
    ChangeEvidenceRecord,
    ReviewPriority,
    SemanticChangeRecord,
)

"""构造有界差异证据，并用确定性规则计算语义变更的审核优先级。"""


REVIEW_PRIORITY_RANK: dict[ReviewPriority, int] = {
    "not_assessed": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}
# 审核优先级到稳定排序分值的固定映射。

# 这些业务类型即使模型只标为 low，也至少进入 medium 审核队列。
MATERIAL_CHANGE_TYPES = frozenset(
    {
        "amount",
        "date_or_term",
        "responsible_party",
        "delivery_scope",
        "payment_term",
        "breach_liability",
        "approval_status",
    }
)

# 纯措辞、格式和无实质变化不得由模型直接升级为高优先级。
NON_MATERIAL_CHANGE_TYPES = frozenset(
    {"wording_only", "formatting_only", "no_material_change"}
)

ALLOWED_CHANGE_TYPES = MATERIAL_CHANGE_TYPES | NON_MATERIAL_CHANGE_TYPES | {
    "contact_or_recipient"
}
# 规则引擎允许接收的完整语义变更类型集合。


def _serialize_value(value: Any, *, max_characters: int) -> str | None:
    """把关键字段值稳定序列化为有界文本。

    Args:
        value: 任意关键字段或结构摘要值。
        max_characters: 序列化文本允许保留的最大字符数。

    Returns:
        有界 JSON 文本、字符串回退值或表示空值的 None。
    """
    if value is None:
        return None
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    rendered = rendered.strip()
    if not rendered:
        return None
    if len(rendered) <= max_characters:
        return rendered
    return rendered[: max_characters - 1].rstrip() + "…"


def _paragraph_units(text: str) -> list[tuple[int, str]]:
    """保留原始段落编号并过滤空白段落。

    Args:
        text: 已标准化但仍保留换行边界的文档文本。

    Returns:
        由一开始计数的段落编号和非空段落文本组成的列表。
    """
    return [
        (index, line.strip())
        for index, line in enumerate(text.splitlines(), start=1)
        if line.strip()
    ]


def _bounded_pair(
    old_value: str | None,
    new_value: str | None,
    *,
    max_characters: int,
) -> tuple[str | None, str | None]:
    """围绕首个字符差异截取一对等位置的有界段落证据。

    Args:
        old_value: 较早版本或候选 A 的段落文本。
        new_value: 较新版本或候选 B 的段落文本。
        max_characters: 每侧证据允许保留的最大字符数。

    Returns:
        保留差异附近上下文的有界旧值和新值。
    """
    old_text = old_value or ""
    new_text = new_value or ""
    if len(old_text) <= max_characters and len(new_text) <= max_characters:
        return old_value, new_value

    shared_prefix = 0
    for left_character, right_character in zip(old_text, new_text, strict=False):
        if left_character != right_character:
            break
        shared_prefix += 1
    context_before = max_characters // 3
    start = max(0, shared_prefix - context_before)

    def slice_value(value: str) -> str | None:
        """按共同差异位置截取单侧有界文本。

        Args:
            value: 等待按外层差异位置截取的单侧文本。

        Returns:
            带可选省略号的有界文本；空输入返回 None。
        """
        if not value:
            return None
        prefix = "…" if start else ""
        available = max_characters - len(prefix)
        excerpt = value[start : start + available]
        if start + available < len(value):
            excerpt = excerpt[: max(0, available - 1)].rstrip() + "…"
        return prefix + excerpt

    return slice_value(old_text), slice_value(new_text)


def build_change_evidence(
    comparison_id: str,
    old_text: str,
    new_text: str,
    old_fields: Mapping[str, Any],
    new_fields: Mapping[str, Any],
    *,
    old_structure: Mapping[str, Any] | None = None,
    new_structure: Mapping[str, Any] | None = None,
    max_key_field_items: int = 20,
    max_text_diff_items: int = 20,
    max_value_characters: int = 1_000,
    max_total_characters: int = 6_000,
) -> list[ChangeEvidenceRecord]:
    """从确定性关键字段和标准化段落差异生成受控语义证据。

    证据值有单项和数量上限，不会把完整文档正文复制到 LangGraph 状态或
    Version Prompt。引用由比较 ID 和确定性位置生成，供模型输出白名单校验。

    Args:
        comparison_id: 当前文件对差异记录的稳定 ID。
        old_text: 较早版本或候选 A 的标准化文本。
        new_text: 较新版本或候选 B 的标准化文本。
        old_fields: 较早版本或候选 A 的结构化关键字段。
        new_fields: 较新版本或候选 B 的结构化关键字段。
        old_structure: 可选较早版本或候选 A 的结构摘要。
        new_structure: 可选较新版本或候选 B 的结构摘要。
        max_key_field_items: 最多保留的关键字段证据数量。
        max_text_diff_items: 最多保留的段落差异证据数量。
        max_value_characters: 单侧证据值的最大字符数。
        max_total_characters: 全部证据引用和值的总字符预算。

    Returns:
        可写入状态并发送给 Version Subagent 的有界差异证据列表。

    Raises:
        ValueError: 比较 ID 为空或任一数量、字符边界配置非法时抛出。
    """
    if not comparison_id.strip():
        raise ValueError("comparison_id 不得为空")
    if max_key_field_items <= 0 or max_text_diff_items <= 0:
        raise ValueError("差异证据数量上限必须大于零")
    if max_value_characters < 32:
        raise ValueError("单项差异证据长度上限不得小于 32")
    if max_total_characters < max_value_characters:
        raise ValueError("差异证据总长度上限不得小于单项长度上限")

    evidence: list[ChangeEvidenceRecord] = []
    total_characters = 0

    def append_if_bounded(item: ChangeEvidenceRecord, *, budget: int) -> bool:
        """仅在字符预算内追加一项差异证据。

        Args:
            item: 等待加入当前结果的差异证据。
            budget: 当前证据阶段允许使用的累计字符预算。

        Returns:
            成功追加返回 True，超出预算且未追加时返回 False。
        """
        nonlocal total_characters
        item_characters = len(item["evidence_ref"]) + sum(
            len(value)
            for value in (item["old_value"], item["new_value"])
            if value is not None
        )
        if total_characters + item_characters > budget:
            return False
        evidence.append(item)
        total_characters += item_characters
        return True

    if old_structure != new_structure:
        append_if_bounded(
            ChangeEvidenceRecord(
                evidence_ref=f"diff:{comparison_id}:structure",
                source="structure",
                old_value=_serialize_value(
                    old_structure,
                    max_characters=max_value_characters,
                ),
                new_value=_serialize_value(
                    new_structure,
                    max_characters=max_value_characters,
                ),
            ),
            budget=max_total_characters // 3,
        )

    changed_field_names = [
        field_name
        for field_name in sorted(set(old_fields) | set(new_fields))
        if old_fields.get(field_name) != new_fields.get(field_name)
    ]
    for index, field_name in enumerate(
        changed_field_names[:max_key_field_items],
        start=1,
    ):
        item = ChangeEvidenceRecord(
                evidence_ref=f"diff:{comparison_id}:field-{index}",
                source="key_field",
                old_value=_serialize_value(
                    old_fields.get(field_name),
                    max_characters=max_value_characters,
                ),
                new_value=_serialize_value(
                    new_fields.get(field_name),
                    max_characters=max_value_characters,
                ),
        )
        # 为正文段落差异至少保留一半 Prompt 预算。
        if not append_if_bounded(item, budget=max_total_characters // 2):
            break

    old_units = _paragraph_units(old_text)
    new_units = _paragraph_units(new_text)
    matcher = SequenceMatcher(
        None,
        [value for _, value in old_units],
        [value for _, value in new_units],
        autojunk=False,
    )
    text_items = 0
    for opcode, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if opcode == "equal":
            continue
        old_block = old_units[old_start:old_end]
        new_block = new_units[new_start:new_end]
        pair_count = max(len(old_block), len(new_block))
        for offset in range(pair_count):
            old_item = old_block[offset] if offset < len(old_block) else None
            new_item = new_block[offset] if offset < len(new_block) else None
            old_number = old_item[0] if old_item else 0
            new_number = new_item[0] if new_item else 0
            old_value, new_value = _bounded_pair(
                old_item[1] if old_item else None,
                new_item[1] if new_item else None,
                max_characters=max_value_characters,
            )
            item = ChangeEvidenceRecord(
                    evidence_ref=(
                        f"diff:{comparison_id}:paragraph-{old_number}-to-{new_number}"
                    ),
                    source="text_diff",
                    old_value=old_value,
                    new_value=new_value,
            )
            if not append_if_bounded(item, budget=max_total_characters):
                return evidence
            text_items += 1
            if text_items >= max_text_diff_items:
                return evidence
    return evidence


def calculate_review_priority(
    semantic_changes: Iterable[SemanticChangeRecord | Mapping[str, Any]],
) -> tuple[ReviewPriority, list[str]]:
    """把经校验的 LLM 分类转换为确定性人工审核优先级。

    LLM 只提供封闭类型、重要性和业务影响。最终优先级由这里的固定映射计算，
    不接受模型返回任何系统动作或优先级字段。

    Args:
        semantic_changes: 已通过输出 Schema 和差异证据白名单校验的语义变更。

    Returns:
        规则计算的最高审核优先级和有界去重原因列表。

    Raises:
        ValueError: 调用方绕过 Schema 并传入非法类型或重要性时抛出。
    """
    highest_score = 0
    reasons: list[str] = []
    for raw_change in semantic_changes:
        change = dict(raw_change)
        change_type = str(change.get("change_type", ""))
        significance = str(change.get("significance", ""))
        if change_type not in ALLOWED_CHANGE_TYPES:
            raise ValueError(f"非法语义变更类型：{change_type}")
        if significance not in {"low", "medium", "high"}:
            raise ValueError(f"非法语义重要性：{significance}")

        score = {"low": 1, "medium": 2, "high": 3}[significance]
        if change_type in MATERIAL_CHANGE_TYPES:
            score = max(score, 2)
        if change_type in NON_MATERIAL_CHANGE_TYPES:
            score = 1
        highest_score = max(highest_score, score)
        if score >= 2:
            impact = str(change.get("business_impact", "")).strip()
            reason = f"{change_type} / {significance}"
            if impact:
                reason += f"：{impact[:300]}"
            reasons.append(reason)

    priority_by_score: dict[int, ReviewPriority] = {
        0: "not_assessed",
        1: "low",
        2: "medium",
        3: "high",
    }
    return priority_by_score[highest_score], list(dict.fromkeys(reasons))[:20]


def highest_group_review_priority(
    diffs: Iterable[Mapping[str, Any]],
    group_id: str,
) -> ReviewPriority:
    """返回版本组全部文件对中的最高语义审核优先级。

    Args:
        diffs: 包含版本组和可选审核优先级的文件对差异记录。
        group_id: 等待汇总的版本组 ID。

    Returns:
        按固定等级顺序选出的版本组最高审核优先级。
    """
    highest: ReviewPriority = "not_assessed"
    for diff in diffs:
        if diff.get("group_id") != group_id:
            continue
        raw_priority = diff.get("review_priority", "not_assessed")
        if raw_priority not in REVIEW_PRIORITY_RANK:
            continue
        priority = cast(ReviewPriority, raw_priority)
        if REVIEW_PRIORITY_RANK[priority] > REVIEW_PRIORITY_RANK[highest]:
            highest = priority
    return highest
