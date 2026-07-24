from __future__ import annotations

import hashlib
from pathlib import Path

from docx import Document

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state

"""本文件验证单个损坏文件经 skip_file 降级后不阻断其余文档的完整治理流程。"""


def create_mixed_parse_state(tmp_path: Path) -> tuple[dict, Path, bytes]:
    """创建一个正常 DOCX 和一个损坏 DOCX 的混合输入。

    Args:
        tmp_path: pytest 为当前测试提供的隔离目录。

    Returns:
        顶层初始状态、损坏文件路径和原始字节。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    valid_document = Document()
    valid_document.add_paragraph("有效合同最终版，金额 CNY 1800。")
    valid_document.save(input_root / "contract_final.docx")
    broken_path = input_root / "contract_broken.docx"
    broken_bytes = b"invalid-docx-content-for-skip-test"
    broken_path.write_bytes(broken_bytes)
    state = create_initial_state(
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
        thread_id="parse-failure-skip",
    )
    return state, broken_path, broken_bytes


def test_parse_failure_skips_only_broken_file_and_continues(
    tmp_path: Path,
) -> None:
    """解析失败应只降级关联文件，同时为正常文件生成推荐和报告。"""
    state, broken_path, broken_bytes = create_mixed_parse_state(tmp_path)
    broken_digest = hashlib.sha256(broken_bytes).hexdigest()

    result = build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "parse-failure-skip"}},
    )

    file_by_name = {record["file_name"]: record for record in result["files"]}
    broken_record = file_by_name["contract_broken.docx"]
    parse_error = next(
        error
        for error in result["errors"]
        if error["category"] == "parse"
        and error["related_file_id"] == broken_record["id"]
    )
    degradation = next(
        item
        for item in result["degradations"]
        if item["error_id"] == parse_error["id"]
    )
    task_statuses = {
        task["task_type"]: task["status"] for task in result["tasks"]
    }

    assert result["run"]["status"] == "partial"
    assert file_by_name["contract_final.docx"]["parse_status"] == "parsed"
    assert broken_record["parse_status"] == "failed"
    assert len(result["documents"]) == 1
    assert len(result["decisions"]) == 1
    assert parse_error["status"] == "fallback_applied"
    assert parse_error["fatal"] is False
    assert degradation["action"] == "skip_file"
    assert degradation["affected_file_ids"] == [broken_record["id"]]
    assert task_statuses["inventory"] == "partial"
    assert "failed" not in task_statuses.values()
    assert hashlib.sha256(broken_path.read_bytes()).hexdigest() == broken_digest
    assert "## 降级项" in result["report"]["report_markdown"]
