from __future__ import annotations

from pathlib import Path

import pytest

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本文件注入长期 Memory 召回故障，验证有限重试后统一执行 no_memory 降级。"""


def create_memory_failure_state(tmp_path: Path) -> dict:
    """创建启用应用数据库和长期 Memory、但没有业务文件的顶层状态。

    Args:
        tmp_path: pytest 为当前测试提供的隔离目录。

    Returns:
        数据库表已初始化且可直接提交给顶层图的完整状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    database_path = tmp_path / "database" / "application.sqlite3"
    engine = create_application_engine(database_path, input_root=input_root)
    Base.metadata.create_all(engine)
    engine.dispose()
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
        memory_config={
            "enabled": True,
            "database_path": str(database_path),
            "recall_limit": 10,
        },
        thread_id="memory-recovery-fallback",
    )


def test_memory_failure_retries_once_then_uses_no_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory 召回连续失败时应重试一次，并以部分完成状态生成可读报告。"""
    state = create_memory_failure_state(tmp_path)
    recall_count = 0

    def raise_recall_failure(self, namespace, *, limit=50):
        """模拟每次短事务召回都发生数据库连接故障。"""
        nonlocal recall_count
        recall_count += 1
        del self, namespace, limit
        raise ConnectionError("injected memory recall failure")

    monkeypatch.setattr(
        "app.nodes.memory.MemoryRepository.recall",
        raise_recall_failure,
    )

    result = build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "memory-recovery-fallback"}},
    )

    memory_error = next(
        error
        for error in result["errors"]
        if error["stage"] == "memory_recall"
        and error["node_name"] == "recall_long_term_memory"
    )
    degradation = next(
        item
        for item in result["degradations"]
        if item["error_id"] == memory_error["id"]
    )
    assert recall_count == 2
    assert memory_error["retry_count"] == 1
    assert memory_error["status"] == "fallback_applied"
    assert memory_error["fatal"] is False
    assert degradation["action"] == "no_memory"
    assert result["memory"]["recalled_items"] == []
    assert result["memory"]["status"] == "ready"
    assert result["run"]["status"] == "partial"
    assert result["report"]["report_path"] is not None
    assert "## 已恢复错误" in result["report"]["report_markdown"]
    assert "## 降级项" in result["report"]["report_markdown"]
