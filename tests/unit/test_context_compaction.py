from __future__ import annotations

from app.services.context_compaction import (
    apply_context_compaction,
    build_context_compaction_plan,
    build_context_summary,
    estimate_compacted_context_tokens,
)
from app.state.models import ContextCompactState, DocumentRecord, PromptState
from app.utils.token_estimation import estimate_text_tokens, estimate_value_tokens

"""本模块验证 Context Compact 的确定性估算、阶段边界、字段保留和安全摘要。"""


def create_enabled_context_state(
    *,
    threshold: int = 1,
    retained_preview_characters: int = 0,
) -> ContextCompactState:
    """创建纯服务测试使用且不访问数据库的 Context Compact 状态。

    Args:
        threshold: 触发压缩的近似 Token 阈值。
        retained_preview_characters: 压缩后保留的文档预览字符数。

    Returns:
        已启用、关闭摘要数据库持久化的 Context Compact 状态。
    """
    return ContextCompactState(
        enabled=True,
        trigger_token_threshold=threshold,
        retained_preview_characters=retained_preview_characters,
        persist_summaries=False,
        database_path=None,
        checkpoint_path=None,
        status="pending",
        current_stage=None,
        estimated_tokens=0,
        summaries=[],
        last_error=None,
    )


def create_prompt(content: str) -> PromptState:
    """创建 Context Compact 单元测试使用的已加载 Prompt。

    Args:
        content: 等待进入测试状态的 Prompt 正文。

    Returns:
        保留固定版本和摘要哈希的 Prompt 状态。
    """
    return PromptState(
        enabled=True,
        version="test-prompt-v1",
        source_path="/safe/prompt.md",
        content=content,
        content_sha256="a" * 64,
        dynamic_rules=["保持只读"],
        status="loaded",
    )


def create_document(preview: str) -> DocumentRecord:
    """创建具有可压缩预览、结构和关键字段的文档记录。

    Args:
        preview: 文档内容预览。

    Returns:
        具有稳定身份字段和产物引用的文档记录。
    """
    return DocumentRecord(
        id="document-1",
        file_id="file-1",
        parser_name="docx-v1",
        content_ref="/safe/normalized/document-1.json",
        content_preview=preview,
        normalized_digest="b" * 64,
        structure_summary={"paragraph_count": 20, "table_count": 2},
        key_fields={"amount": "1200", "date": "2026-07-23"},
        warnings=["测试警告"],
    )


def test_token_estimation_is_deterministic_and_conservative_for_chinese() -> None:
    """中文字符应按保守规则估算，结构化值重复估算结果必须一致。"""
    text = "abcd中文"

    assert estimate_text_tokens(text) == 3
    assert estimate_value_tokens({"text": text}) == estimate_value_tokens({"text": text})


def test_after_inventory_only_discards_prompt_content() -> None:
    """Inventory 后即使触发压缩也不得改写文档记录。"""
    prompt = create_prompt("系统规则" * 100)
    document = create_document("业务正文预览" * 100)
    context = create_enabled_context_state()
    plan = build_context_compaction_plan(
        stage="after_inventory",
        prompt=prompt,
        documents=[document],
        context_compact=context,
    )

    compacted_prompt, compacted_documents, payload = apply_context_compaction(
        plan=plan,
        prompt=prompt,
        documents=[document],
        retained_preview_characters=0,
    )

    assert plan["should_compact"] is True
    assert plan["compact_document_ids"] == []
    assert compacted_prompt["content"] == ""
    assert compacted_prompt["content_sha256"] == prompt["content_sha256"]
    assert compacted_documents == [document]
    assert payload["removed_documents"] == []
    assert prompt["content"] != ""


def test_after_evidence_moves_verbose_document_fields_but_keeps_identity() -> None:
    """Evidence 后可移出详情，但 content_ref、哈希、文件关联和警告必须保留。"""
    prompt = create_prompt("")
    document = create_document("长预览内容" * 200)
    context = create_enabled_context_state(retained_preview_characters=4)
    plan = build_context_compaction_plan(
        stage="after_evidence",
        prompt=prompt,
        documents=[document],
        context_compact=context,
    )
    before_tokens = plan["estimated_tokens_before"]

    compacted_prompt, compacted_documents, payload = apply_context_compaction(
        plan=plan,
        prompt=prompt,
        documents=[document],
        retained_preview_characters=4,
    )
    compacted = compacted_documents[0]
    after_tokens = estimate_compacted_context_tokens(
        compacted_prompt,
        compacted_documents,
    )

    assert compacted["id"] == document["id"]
    assert compacted["file_id"] == document["file_id"]
    assert compacted["content_ref"] == document["content_ref"]
    assert compacted["normalized_digest"] == document["normalized_digest"]
    assert compacted["warnings"] == document["warnings"]
    assert compacted["content_preview"] == document["content_preview"][:4]
    assert compacted["structure_summary"] == {"compacted": True}
    assert compacted["key_fields"] == {}
    assert payload["removed_documents"][0]["key_fields"] == document["key_fields"]
    assert after_tokens < before_tokens


def test_threshold_or_disabled_state_skips_compaction() -> None:
    """关闭功能或未超过阈值时计划必须保持上下文不变。"""
    prompt = create_prompt("短规则")
    document = create_document("短预览")
    disabled_context = create_enabled_context_state()
    disabled_context["enabled"] = False
    high_threshold_context = create_enabled_context_state(threshold=1_000_000)

    disabled_plan = build_context_compaction_plan(
        stage="after_evidence",
        prompt=prompt,
        documents=[document],
        context_compact=disabled_context,
    )
    threshold_plan = build_context_compaction_plan(
        stage="after_evidence",
        prompt=prompt,
        documents=[document],
        context_compact=high_threshold_context,
    )

    assert disabled_plan["should_compact"] is False
    assert threshold_plan["should_compact"] is False


def test_context_summary_contains_only_fixed_bounded_description() -> None:
    """Context Summary 不得复制被移出的正文或完整 Prompt。"""
    leaked_body = "DOCUMENT-BODY-MUST-NOT-ENTER-SUMMARY"
    leaked_prompt = "FULL-MODEL-PROMPT-MUST-NOT-ENTER-SUMMARY"
    prompt = create_prompt(leaked_prompt * 20)
    document = create_document(leaked_body * 20)
    context = create_enabled_context_state()
    plan = build_context_compaction_plan(
        stage="after_evidence",
        prompt=prompt,
        documents=[document],
        context_compact=context,
    )

    summary = build_context_summary(
        run_id="run-context-unit",
        plan=plan,
        estimated_tokens_after=120,
        compaction_index=1,
    )

    assert leaked_body not in summary["summary"]
    assert leaked_prompt not in summary["summary"]
    assert summary["stage"] == "after_evidence"
    assert summary["estimated_tokens"] == 120
