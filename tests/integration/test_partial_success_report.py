from __future__ import annotations

from pathlib import Path

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state

"""本文件集成验证解析降级后的部分成功状态、Task 统计和独立恢复报告章节。"""


def create_partial_report_state(tmp_path: Path) -> dict:
    """创建包含一个损坏 DOCX 的完整顶层治理输入。

    Args:
        tmp_path: pytest 为当前测试提供的隔离目录。

    Returns:
        可直接提交顶层图且会触发 parse/skip_file 恢复的初始状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "broken.docx").write_bytes(b"not-a-valid-docx")
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
        thread_id="partial-success-report",
    )


def test_partial_success_is_reported_separately_from_failure(
    tmp_path: Path,
) -> None:
    """文件级安全降级应生成 partial 运行和报告，不能产生 failed Task。"""
    state = create_partial_report_state(tmp_path)

    result = build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "partial-success-report"}},
    )

    task_statuses = {
        task["task_type"]: task["status"] for task in result["tasks"]
    }
    markdown = result["report"]["report_markdown"]
    recovered_errors = [
        error
        for error in result["errors"]
        if error["status"] in {"recovered", "fallback_applied"}
    ]

    assert result["run"]["status"] == "partial"
    assert task_statuses["inventory"] == "partial"
    assert "failed" not in task_statuses.values()
    assert task_statuses["report"] == "completed"
    assert "结果为部分完成" in result["report"]["summary"]
    assert "## 已恢复错误" in markdown
    assert "## 降级项" in markdown
    assert "`skip_file`" in markdown
    assert result["report"]["recovered_error_ids"] == [
        error["id"] for error in recovered_errors
    ]
    assert result["report"]["degradation_ids"] == [
        degradation["id"] for degradation in result["degradations"]
    ]
    assert Path(result["report"]["report_path"]).read_text(
        encoding="utf-8"
    ).rstrip() == markdown
