from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state

"""本文件在两个 Context Compact 安全点注入失败并验证 keep_context 降级不丢失事实。"""


def create_context_failure_state(tmp_path: Path) -> dict:
    """创建必定进入两次 Context Compact 安全点的单文档治理状态。

    Args:
        tmp_path: pytest 为当前测试提供的隔离目录。

    Returns:
        启用低阈值 Context Compact 且关闭摘要持久化的完整状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    document = Document()
    document.add_paragraph("上下文恢复测试合同，金额 CNY 2600。" + "长期条款。" * 300)
    document.save(input_root / "context_contract.docx")
    return create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.0,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": False,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(tmp_path / "artifacts"),
            "report_root": str(tmp_path / "reports"),
        },
        context_compact_config={
            "enabled": True,
            "trigger_token_threshold": 1,
            "retained_preview_characters": 0,
            "persist_summaries": False,
        },
        thread_id="context-recovery-fallback",
    )


def test_context_failure_keeps_original_context_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两次上下文估算失败都应保留文档引用，并以 keep_context 部分完成。"""
    state = create_context_failure_state(tmp_path)
    estimate_count = 0

    def raise_context_estimation_failure(**kwargs):
        """模拟 Context Compact 计划估算中的确定性输入错误。"""
        nonlocal estimate_count
        estimate_count += 1
        del kwargs
        raise ValueError("injected context compaction failure")

    monkeypatch.setattr(
        "app.nodes.context_compact.build_context_compaction_plan",
        raise_context_estimation_failure,
    )

    result = build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "context-recovery-fallback"}},
    )

    context_errors = [
        error
        for error in result["errors"]
        if error["category"] == "context"
        and error["node_name"] == "estimate_context_tokens"
    ]
    context_degradations = [
        item for item in result["degradations"] if item["action"] == "keep_context"
    ]
    assert estimate_count == 2
    assert len(context_errors) == 2
    assert all(error["status"] == "fallback_applied" for error in context_errors)
    assert len(context_degradations) == 2
    assert result["run"]["status"] == "partial"
    assert len(result["documents"]) == 1
    assert result["documents"][0]["content_preview"]
    assert result["documents"][0]["content_ref"]
    assert len(result["decisions"]) == 1
    assert result["context_compact"]["summaries"] == []
    assert not any(error["fatal"] for error in result["errors"])
    assert "## 降级项" in result["report"]["report_markdown"]
