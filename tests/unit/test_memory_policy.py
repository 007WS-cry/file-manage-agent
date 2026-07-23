from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.memory_policy import (
    RECALLED_CHOICE_SCORE_BOOST,
    apply_recalled_choices,
    capture_evidence_memory,
    capture_human_choice_memory,
    create_memory_item,
    derive_memory_namespace,
)
from app.state.factories import create_memory_state
from app.state.models import MemoryState, RequestState

"""本模块验证 Memory 命名空间隔离、最小化捕获、凭据拒绝和有界历史偏好。"""


def test_namespace_is_hashed_without_original_directory(tmp_path: Path) -> None:
    """工作空间命名空间不得包含原始绝对目录文本。"""
    input_root = tmp_path / "客户甲" / "合同"

    namespace = derive_memory_namespace(input_root)

    assert namespace.startswith("workspace:")
    assert str(input_root).casefold() not in namespace.casefold()
    assert len(namespace) == len("workspace:") + 64


def test_memory_database_cannot_share_checkpoint_file(tmp_path: Path) -> None:
    """应用数据库和 SQLite Checkpointer 必须使用不同文件。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    shared_database = tmp_path / "shared.sqlite3"
    request = RequestState(
        root_directory=str(input_root),
        recursive=True,
        allowed_extensions=[".docx"],
        max_files=20,
        grouping_similarity_threshold=0.72,
        auto_select_threshold=0.82,
        pdf_match_threshold=0.82,
        delivery_log_path=None,
        use_llm_summary=False,
    )

    with pytest.raises(ValueError, match="不得与 LangGraph checkpoint 共用"):
        create_memory_state(
            request,
            {"enabled": True, "database_path": str(shared_database)},
            checkpoint_path=shared_database,
        )


def test_memory_item_rejects_long_body_and_api_key() -> None:
    """安全校验必须拒绝长正文和疑似 API Key。"""
    common = {
        "namespace": f"workspace:{'a' * 64}",
        "scope": "long_term",
        "kind": "governance_preference",
        "structured_data": {"stage": "recommendation"},
        "artifact_refs": [],
        "source_run_id": "run-policy",
        "confirmed_by_human": False,
        "confidence": 1.0,
    }

    with pytest.raises(ValueError, match="长度"):
        create_memory_item(summary="正文" * 200, **common)
    with pytest.raises(ValueError, match="凭据"):
        create_memory_item(summary="密钥 sk-super-secret-123456789", **common)


def test_evidence_memory_uses_only_fixed_summary_and_ids() -> None:
    """证据 Memory 不得复制匹配信号、收件人或原始证据引用。"""
    leaked_body = "DOCUMENT-LONG-BODY-SHOULD-NOT-PERSIST"
    leaked_recipient = "customer-private@example.test"
    leaked_reference = "prompt://FULL-MODEL-PROMPT"
    memory = MemoryState(
        enabled=True,
        namespace=f"workspace:{'b' * 64}",
        database_path="memory.sqlite3",
        checkpoint_path=None,
        recall_limit=50,
        status="ready",
        recalled_items=[],
        short_term_items=[],
        pending_long_term_items=[],
        persisted_item_ids=[],
        last_error=None,
    )

    result = capture_evidence_memory(
        memory,
        source_run_id="run-evidence",
        pdf_exports=[
            {
                "id": "pdf-relation-1",
                "group_id": "group-1",
                "pdf_file_id": "file-pdf",
                "source_file_id": "file-source",
                "match_score": 0.96,
                "matched_signals": [leaked_body],
                "confidence": 0.96,
            }
        ],
        deliveries=[
            {
                "id": "delivery-1",
                "group_id": "group-1",
                "file_id": "file-source",
                "evidence_source": "local_log",
                "sent_at": "2026-07-23T08:00:00+00:00",
                "recipient_label": leaked_recipient,
                "evidence_ref": leaked_reference,
                "match_method": "sha256",
                "customer_confirmed": True,
                "confidence": 1.0,
            }
        ],
        confidence_threshold=0.82,
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert len(result["short_term_items"]) == 1
    assert len(result["pending_long_term_items"]) == 2
    assert leaked_body not in serialized
    assert leaked_recipient not in serialized
    assert leaked_reference not in serialized


def test_human_choice_memory_ignores_free_text_by_contract() -> None:
    """人工选择 Memory 只保存组和文件 ID，不接收自由文本审核说明。"""
    memory = MemoryState(
        enabled=True,
        namespace=f"workspace:{'c' * 64}",
        database_path="memory.sqlite3",
        checkpoint_path=None,
        recall_limit=50,
        status="ready",
        recalled_items=[],
        short_term_items=[],
        pending_long_term_items=[],
        persisted_item_ids=[],
        last_error=None,
    )

    result = capture_human_choice_memory(
        memory,
        source_run_id="run-human",
        version_groups=[
            {
                "id": "group-1",
                "label": "禁止保存的自由文本标签",
                "file_ids": ["file-a", "file-b"],
                "grouping_signals": ["禁止保存的正文信号"],
                "confidence": 0.9,
            }
        ],
        selections={"group-1": "file-b"},
    )
    item = result["pending_long_term_items"][0]

    assert item["structured_data"] == {
        "group_id": "group-1",
        "selected_file_id": "file-b",
    }
    assert item["confirmed_by_human"] is True
    assert "自由文本" not in json.dumps(item, ensure_ascii=False)


def test_recalled_human_choice_only_applies_bounded_exact_candidate_boost() -> None:
    """历史人工选择只能为仍存在的同组候选增加固定小分值。"""
    recalled = create_memory_item(
        namespace=f"workspace:{'d' * 64}",
        scope="long_term",
        kind="confirmed_version_choice",
        summary="用户已明确确认该版本组的主版本。",
        structured_data={"group_id": "group-1", "selected_file_id": "file-b"},
        artifact_refs=[],
        source_run_id="run-old",
        confirmed_by_human=True,
        confidence=1.0,
    )
    decision = {
        "id": "decision-1",
        "group_id": "group-1",
        "candidate_scores": {"file-a": 0.7, "file-b": 0.72},
        "recommended_file_id": None,
        "reasons": [],
        "confidence": 0.0,
        "needs_human_review": False,
        "selected_by": "unresolved",
        "preserve_file_ids": [],
    }

    result = apply_recalled_choices([decision], [recalled])

    assert result[0]["candidate_scores"]["file-a"] == 0.7
    assert result[0]["candidate_scores"]["file-b"] == pytest.approx(
        0.72 + RECALLED_CHOICE_SCORE_BOOST
    )
    assert decision["candidate_scores"]["file-b"] == 0.72
