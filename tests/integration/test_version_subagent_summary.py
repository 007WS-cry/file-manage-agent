from __future__ import annotations

from pathlib import Path

from docx import Document

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state

"""本文件验证 Version Subagent 只升级摘要及来源，不改变任何确定性比较事实。"""

# 验证摘要升级前后必须完全一致的确定性 DiffRecord 字段。
DETERMINISTIC_DIFF_FIELDS = (
    "id",
    "group_id",
    "file_a_id",
    "file_b_id",
    "older_file_id",
    "newer_file_id",
    "structural_similarity",
    "content_similarity",
    "key_changes",
    "ordering_signals",
    "confidence",
)


def write_docx(path: Path, text: str) -> None:
    """写入版本摘要集成测试使用的单段 DOCX。

    Args:
        path: 测试文件输出路径。
        text: 文档正文。
    """
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def create_summary_state(
    input_root: Path,
    output_root: Path,
    *,
    use_llm_summary: bool,
) -> dict:
    """创建启用或关闭 Version Subagent 摘要的可比较顶层状态。

    Args:
        input_root: 两个候选版本所在的只读输入目录。
        output_root: 当前运行独立使用的产物和报告根目录。
        use_llm_summary: 是否允许 Version Subagent 替换解释摘要。

    Returns:
        使用安全 Mock Provider 的完整初始治理状态。
    """
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
            "use_llm_summary": use_llm_summary,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(output_root / "artifacts"),
            "report_root": str(output_root / "reports"),
        },
    )


def test_version_subagent_only_replaces_summary_and_records_source(
    tmp_path: Path,
) -> None:
    """成功 Mock 输出应登记协议来源，同时保持方向、变化和置信度不变。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    shared_text = "合同共同条款 A、B、C。" * 80
    write_docx(input_root / "proposal_v1.docx", f"金额 CNY 1000。{shared_text}")
    write_docx(input_root / "proposal_v2.docx", f"金额 CNY 1200。{shared_text}")

    baseline = build_file_governance_graph().invoke(
        create_summary_state(
            input_root,
            tmp_path / "baseline",
            use_llm_summary=False,
        ),
        config={"configurable": {"thread_id": "version-summary-baseline"}},
    )
    enhanced = build_file_governance_graph().invoke(
        create_summary_state(
            input_root,
            tmp_path / "enhanced",
            use_llm_summary=True,
        ),
        config={"configurable": {"thread_id": "version-summary-enhanced"}},
    )

    baseline_by_id = {diff["id"]: diff for diff in baseline["diffs"]}
    assert enhanced["diffs"]
    for diff in enhanced["diffs"]:
        baseline_diff = baseline_by_id[diff["id"]]
        assert diff["summary"] == "Mock LLM 已生成结构化摘要。"
        assert diff["summary_source"] == "version_subagent"
        assert diff["summary_message_id"]
        assert diff["summary_artifact_ref"] is None
        assert all(
            diff[field_name] == baseline_diff[field_name]
            for field_name in DETERMINISTIC_DIFF_FIELDS
        )
        assert any(
            message["message_id"] == diff["summary_message_id"]
            and message["sender"] == "version-subagent"
            and message["message_type"] == "result"
            for message in enhanced["team_messages"]
        )

    report = enhanced["report"]["report_markdown"]
    assert "### 关键修改摘要" in report
    assert "Version Subagent" in report
