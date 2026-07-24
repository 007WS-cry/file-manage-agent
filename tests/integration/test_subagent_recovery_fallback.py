from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state

"""本文件在顶层流程注入 Content Subagent 崩溃并验证 coordinator 恢复降级。"""


def create_subagent_fallback_state(tmp_path: Path) -> dict:
    """创建会分派 Content Subagent 的单文档治理状态。

    Args:
        tmp_path: pytest 为当前测试提供的隔离目录。

    Returns:
        使用离线 Mock Provider 的完整顶层状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    document = Document()
    document.add_paragraph("Subagent 故障注入合同，金额 CNY 2200。")
    document.save(input_root / "subagent_contract.docx")
    return create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
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
        thread_id="subagent-recovery-fallback",
    )


def test_subagent_exception_uses_coordinator_and_recovery_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content Subagent 崩溃后应使用协调者结果并登记统一降级记录。"""
    state = create_subagent_fallback_state(tmp_path)
    invoke_count = 0

    def invoke_crashing_content_subgraph(subgraph_state):
        """模拟 Content Subagent 在读取最小输入后抛出未处理异常。"""
        nonlocal invoke_count
        invoke_count += 1
        del subgraph_state
        raise RuntimeError("injected content subagent failure")

    monkeypatch.setattr(
        "app.nodes.team_orchestration.content_subagent_graph",
        SimpleNamespace(invoke=invoke_crashing_content_subgraph),
    )

    result = build_file_governance_graph().invoke(
        state,
        config={
            "configurable": {"thread_id": "subagent-recovery-fallback"},
            "recursion_limit": 100,
        },
    )

    recovered_errors = [
        error
        for error in result["errors"]
        if error.get("fallback") == "coordinator"
        and error["status"] == "fallback_applied"
    ]
    coordinator_degradations = [
        degradation
        for degradation in result["degradations"]
        if degradation["action"] == "coordinator"
    ]
    assert invoke_count == 1
    assert recovered_errors
    assert coordinator_degradations
    assert result["run"]["status"] == "partial"
    assert len(result["documents"]) == 1
    assert len(result["decisions"]) == 1
    assert any(
        message["message_type"] == "result"
        and "确定性内容概览" in message["summary"]
        for message in result["team_messages"]
    )
    assert not any(error["fatal"] for error in result["errors"])
    assert "## 降级项" in result["report"]["report_markdown"]
