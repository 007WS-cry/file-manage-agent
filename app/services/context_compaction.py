from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from app.state.models import (
    ContextCompactionPlanState,
    ContextCompactState,
    ContextSummaryState,
    DocumentRecord,
    PromptState,
)
from app.utils.runtime import utc_now_iso
from app.utils.token_estimation import estimate_value_tokens

"""本模块以纯函数规划和执行上下文压缩，保证治理事实与人工选择字段不被改写。"""


# Context Compact 只允许在 ORM 约束声明的两个固定阶段运行。
ContextCompactStage = Literal["after_inventory", "after_evidence"]

# 默认在估算上下文超过一万二千 Token 时触发压缩。
DEFAULT_CONTEXT_COMPACT_TRIGGER_TOKENS = 12_000

# 默认不在图状态继续保留文档正文预览，完整内容仍可由 content_ref 重建。
DEFAULT_RETAINED_PREVIEW_CHARACTERS = 0


def create_disabled_context_compact_state() -> ContextCompactState:
    """创建不会估算、写产物或访问数据库的兼容性 Context Compact 状态。

    Returns:
        状态为 ``disabled`` 且摘要列表为空的新对象。
    """
    return ContextCompactState(
        enabled=False,
        trigger_token_threshold=DEFAULT_CONTEXT_COMPACT_TRIGGER_TOKENS,
        retained_preview_characters=DEFAULT_RETAINED_PREVIEW_CHARACTERS,
        persist_summaries=False,
        database_path=None,
        checkpoint_path=None,
        status="disabled",
        current_stage=None,
        estimated_tokens=0,
        summaries=[],
        last_error=None,
    )


def copy_context_summary(
    summary: Mapping[str, Any],
) -> ContextSummaryState:
    """深复制一个有界 Context Summary 状态。

    Args:
        summary: 等待复制的摘要映射。

    Returns:
        产物引用列表已解除可变共享的摘要状态。
    """
    return ContextSummaryState(
        id=str(summary["id"]),
        run_id=str(summary["run_id"]),
        stage=cast(ContextCompactStage, summary["stage"]),
        summary=str(summary["summary"]),
        artifact_refs=[str(reference) for reference in summary.get("artifact_refs", [])],
        estimated_tokens=int(summary["estimated_tokens"]),
        compaction_index=int(summary["compaction_index"]),
        created_at=str(summary["created_at"]),
    )


def copy_context_compact_state(
    context_compact: Mapping[str, Any] | None,
) -> ContextCompactState:
    """复制 Context Compact 状态并为旧 checkpoint 补齐关闭默认值。

    Args:
        context_compact: 当前顶层状态中的可选 Context Compact 映射。

    Returns:
        所有嵌套列表均与输入解除引用关系的完整状态。
    """
    if context_compact is None:
        return create_disabled_context_compact_state()
    enabled = bool(context_compact.get("enabled", False))
    raw_stage = context_compact.get("current_stage")
    return ContextCompactState(
        enabled=enabled,
        trigger_token_threshold=int(
            context_compact.get(
                "trigger_token_threshold",
                DEFAULT_CONTEXT_COMPACT_TRIGGER_TOKENS,
            )
        ),
        retained_preview_characters=int(
            context_compact.get(
                "retained_preview_characters",
                DEFAULT_RETAINED_PREVIEW_CHARACTERS,
            )
        ),
        persist_summaries=bool(context_compact.get("persist_summaries", False)),
        database_path=(
            str(context_compact["database_path"])
            if context_compact.get("database_path") is not None
            else None
        ),
        checkpoint_path=(
            str(context_compact["checkpoint_path"])
            if context_compact.get("checkpoint_path") is not None
            else None
        ),
        status=cast(
            Literal["disabled", "pending", "ready", "failed"],
            context_compact.get("status", "pending" if enabled else "disabled"),
        ),
        current_stage=(
            cast(ContextCompactStage, raw_stage)
            if raw_stage in {"after_inventory", "after_evidence"}
            else None
        ),
        estimated_tokens=int(context_compact.get("estimated_tokens", 0)),
        summaries=[
            copy_context_summary(summary) for summary in context_compact.get("summaries", [])
        ],
        last_error=(
            str(context_compact["last_error"])
            if context_compact.get("last_error") is not None
            else None
        ),
    )


def copy_prompt_state(prompt: Mapping[str, Any]) -> PromptState:
    """复制 Prompt 状态及动态规则列表。

    Args:
        prompt: 当前顶层治理 Prompt 状态。

    Returns:
        与输入解除可变共享的 Prompt 状态。
    """
    return PromptState(
        enabled=bool(prompt["enabled"]),
        version=str(prompt["version"]),
        source_path=(str(prompt["source_path"]) if prompt.get("source_path") is not None else None),
        content=str(prompt.get("content", "")),
        content_sha256=(
            str(prompt["content_sha256"]) if prompt.get("content_sha256") is not None else None
        ),
        dynamic_rules=[str(rule) for rule in prompt.get("dynamic_rules", [])],
        status=cast(
            Literal["pending", "loaded", "disabled", "failed"],
            prompt["status"],
        ),
    )


def copy_document_record(document: Mapping[str, Any]) -> DocumentRecord:
    """深复制标准化文档记录中可能被压缩的嵌套字段。

    Args:
        document: 当前顶层状态中的文档记录。

    Returns:
        结构摘要、关键字段和警告均解除共享的文档记录。
    """
    return DocumentRecord(
        id=str(document["id"]),
        file_id=str(document["file_id"]),
        parser_name=str(document["parser_name"]),
        content_ref=str(document["content_ref"]),
        content_preview=str(document.get("content_preview", "")),
        normalized_digest=str(document["normalized_digest"]),
        structure_summary=dict(document.get("structure_summary", {})),
        key_fields=dict(document.get("key_fields", {})),
        warnings=[str(warning) for warning in document.get("warnings", [])],
    )


def build_context_compaction_plan(
    *,
    stage: ContextCompactStage,
    prompt: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
    context_compact: Mapping[str, Any] | None,
) -> ContextCompactionPlanState:
    """估算当前上下文并生成不触碰治理事实的压缩计划。

    Inventory 后只允许释放已经完成加载校验的 Prompt 正文；Evidence 后允许
    同时移出后续 Recommendation 不再消费的文档预览、结构详情和关键字段。
    文件、哈希、content_ref、版本关系、证据、推荐和人工审核不属于本计划。

    Args:
        stage: 当前固定压缩阶段。
        prompt: 顶层 Prompt 状态。
        documents: 当前标准化文档记录。
        context_compact: 顶层 Context Compact 配置与历史状态。

    Returns:
        包含压缩前估算、可回收估算和目标文档 ID 的确定性计划。

    Raises:
        ValueError: 阶段不在固定白名单内时抛出。
    """
    if stage not in {"after_inventory", "after_evidence"}:
        raise ValueError(f"不支持的 Context Compact 阶段：{stage}")
    normalized_context = copy_context_compact_state(context_compact)
    normalized_prompt = copy_prompt_state(prompt)
    normalized_documents = [copy_document_record(document) for document in documents]
    estimated_tokens_before = estimate_value_tokens(
        {
            "prompt": normalized_prompt,
            "documents": normalized_documents,
        }
    )
    compact_prompt_content = bool(normalized_prompt.get("content"))
    reclaimable_tokens = (
        estimate_value_tokens(normalized_prompt["content"]) if compact_prompt_content else 0
    )
    compact_document_ids: list[str] = []
    if stage == "after_evidence":
        retained_characters = normalized_context["retained_preview_characters"]
        for document in normalized_documents:
            removable_payload = {
                "content_preview": document["content_preview"][retained_characters:],
                "structure_summary": document["structure_summary"],
                "key_fields": document["key_fields"],
            }
            removable_tokens = estimate_value_tokens(removable_payload)
            if removable_tokens > 0:
                compact_document_ids.append(document["id"])
                reclaimable_tokens += removable_tokens

    should_compact = (
        normalized_context["enabled"]
        and estimated_tokens_before > normalized_context["trigger_token_threshold"]
        and reclaimable_tokens > 0
    )
    return ContextCompactionPlanState(
        stage=stage,
        estimated_tokens_before=estimated_tokens_before,
        reclaimable_tokens=reclaimable_tokens,
        should_compact=should_compact,
        compact_prompt_content=compact_prompt_content and should_compact,
        compact_document_ids=(compact_document_ids if should_compact else []),
    )


def apply_context_compaction(
    *,
    plan: ContextCompactionPlanState,
    prompt: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
    retained_preview_characters: int,
) -> tuple[PromptState, list[DocumentRecord], dict[str, Any]]:
    """按照既定计划压缩 Prompt 和文档上下文，并构造未跟踪产物载荷。

    Args:
        plan: 已由估算函数生成且允许执行的压缩计划。
        prompt: 当前 Prompt 状态。
        documents: 当前标准化文档记录。
        retained_preview_characters: 每个文档仍保留的预览字符数。

    Returns:
        压缩后的 Prompt、文档列表及只含被移出文档字段的产物载荷。

    Raises:
        ValueError: 计划未允许压缩或保留字符数为负数时抛出。
    """
    if not plan["should_compact"]:
        raise ValueError("当前计划未允许执行 Context Compact")
    if retained_preview_characters < 0:
        raise ValueError("retained_preview_characters 不得为负数")

    compacted_prompt = copy_prompt_state(prompt)
    if plan["compact_prompt_content"]:
        compacted_prompt["content"] = ""
        compacted_prompt["dynamic_rules"] = []

    target_document_ids = set(plan["compact_document_ids"])
    compacted_documents: list[DocumentRecord] = []
    removed_documents: list[dict[str, Any]] = []
    for raw_document in documents:
        document = copy_document_record(raw_document)
        if document["id"] in target_document_ids:
            removed_documents.append(
                {
                    "id": document["id"],
                    "content_preview": document["content_preview"],
                    "structure_summary": dict(document["structure_summary"]),
                    "key_fields": dict(document["key_fields"]),
                }
            )
            document["content_preview"] = document["content_preview"][:retained_preview_characters]
            document["structure_summary"] = {"compacted": True}
            document["key_fields"] = {}
        compacted_documents.append(document)

    payload = {
        "schema_version": "1.0",
        "stage": plan["stage"],
        "removed_documents": removed_documents,
        "prompt_content_discarded": plan["compact_prompt_content"],
    }
    return compacted_prompt, compacted_documents, payload


def build_context_summary(
    *,
    run_id: str,
    plan: ContextCompactionPlanState,
    estimated_tokens_after: int,
    compaction_index: int,
    artifact_refs: Sequence[str] = (),
) -> ContextSummaryState:
    """由固定模板生成不含正文和 Prompt 的 Context Summary。

    Args:
        run_id: 当前治理运行 ID。
        plan: 实际执行的压缩计划。
        estimated_tokens_after: 压缩后的近似上下文 Token 数。
        compaction_index: 当前运行内从一开始递增的压缩序号。
        artifact_refs: 可选受控中间产物引用。

    Returns:
        具有确定性 ID 和有界摘要的 Context Summary。

    Raises:
        ValueError: 运行 ID、序号或 Token 数不合法时抛出。
    """
    if not run_id.strip():
        raise ValueError("run_id 不得为空")
    if compaction_index < 1:
        raise ValueError("compaction_index 必须大于零")
    if estimated_tokens_after < 0:
        raise ValueError("estimated_tokens_after 不得为负数")
    compacted_document_count = len(plan["compact_document_ids"])
    if plan["stage"] == "after_inventory":
        summary_text = "Inventory 后已释放后续流程不再读取的 Prompt 正文。"
    else:
        summary_text = (
            f"Evidence 后已压缩 {compacted_document_count} 个文档上下文字段，"
            "版本、证据、推荐与人工审核事实保持不变。"
        )
    identity = f"{run_id}\x1f{plan['stage']}\x1f{compaction_index}"
    summary_id = "context-" + hashlib.sha256(identity.encode()).hexdigest()
    return ContextSummaryState(
        id=summary_id,
        run_id=run_id,
        stage=plan["stage"],
        summary=summary_text,
        artifact_refs=[str(reference) for reference in artifact_refs],
        estimated_tokens=estimated_tokens_after,
        compaction_index=compaction_index,
        created_at=utc_now_iso(),
    )


def estimate_compacted_context_tokens(
    prompt: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
) -> int:
    """估算压缩后 Prompt 与文档上下文的 Token 数。

    Args:
        prompt: 已压缩或原样保留的 Prompt 状态。
        documents: 已压缩或原样保留的文档记录。

    Returns:
        两类上下文稳定 JSON 表示的近似 Token 数。
    """
    return estimate_value_tokens(
        {
            "prompt": copy_prompt_state(prompt),
            "documents": [copy_document_record(document) for document in documents],
        }
    )


def append_context_summary(
    context_compact: Mapping[str, Any] | None,
    summary: ContextSummaryState,
) -> ContextCompactState:
    """把已完成的摘要幂等加入顶层 Context Compact 状态。

    Args:
        context_compact: 当前 Context Compact 状态。
        summary: 已补齐产物引用并完成持久化尝试的摘要。

    Returns:
        状态为 ready、估算值已更新且摘要 ID 不重复的状态副本。
    """
    result = copy_context_compact_state(context_compact)
    summary_ids = {item["id"] for item in result["summaries"]}
    if summary["id"] not in summary_ids:
        result["summaries"].append(copy_context_summary(summary))
    result["status"] = "ready"
    result["current_stage"] = summary["stage"]
    result["estimated_tokens"] = summary["estimated_tokens"]
    result["last_error"] = None
    return result
