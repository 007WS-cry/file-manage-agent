from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document

from app.graphs.file_governance import build_file_governance_graph
from app.nodes import subgraphs_nodes
from app.state.factories import create_initial_state

"""本文件通过瞬时子图异常验证有限重试、成功重放和恢复终态不会重复登记。"""


def create_retry_state(tmp_path: Path) -> dict:
    """创建包含一个可正常解析 DOCX 的顶层治理状态。

    Args:
        tmp_path: pytest 为当前测试提供的隔离目录。

    Returns:
        可直接提交顶层图的完整状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    document = Document()
    document.add_paragraph("瞬时重试合同，金额 CNY 1200。")
    document.save(input_root / "contract_final.docx")
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
        thread_id="transient-retry-replay",
    )


def test_transient_inventory_failure_retries_once_and_replays_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inventory 首次超时后应有限重试一次，并以同一错误 ID 恢复。"""
    state = create_retry_state(tmp_path)
    original_invoke = subgraphs_nodes.inventory_graph.invoke
    invoke_count = 0

    def invoke_with_transient_timeout(*args, **kwargs):
        """首次注入超时，后续调用执行真实 Inventory 子图。"""
        nonlocal invoke_count
        invoke_count += 1
        if invoke_count == 1:
            raise TimeoutError("transient inventory timeout")
        return original_invoke(*args, **kwargs)

    monkeypatch.setattr(
        subgraphs_nodes.inventory_graph,
        "invoke",
        invoke_with_transient_timeout,
    )

    result = build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "transient-retry-replay"}},
    )

    boundary_errors = [
        error
        for error in result["errors"]
        if error["node_name"] == "run_inventory_subgraph"
    ]
    inventory_execution = next(
        execution
        for execution in result["node_executions"]
        if execution["node_name"] == "run_inventory_subgraph"
    )
    assert invoke_count == 2
    assert len(boundary_errors) == 1
    assert boundary_errors[0]["status"] == "recovered"
    assert boundary_errors[0]["retry_count"] == 1
    assert boundary_errors[0]["fatal"] is False
    assert inventory_execution["status"] == "succeeded"
    assert inventory_execution["attempt_count"] == 2
    assert Path(inventory_execution["state_update_ref"] or "").is_file()
    assert result["run"]["status"] == "partial"
    assert result["degradations"] == []
    assert "## 已恢复错误" in result["report"]["report_markdown"]
