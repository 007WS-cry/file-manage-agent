from __future__ import annotations

from pathlib import Path

from docx import Document
from langgraph.types import Command

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state

"""本模块对比启用与关闭 Context Compact 的完整治理结果，验证决策事实严格不变。"""


def create_docx(path: Path, text: str) -> None:
    """创建决策不变性测试使用的最小 DOCX。

    Args:
        path: 文档输出路径。
        text: 写入正文段落的测试文本。
    """
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def create_invariance_state(
    input_root: Path,
    artifact_root: Path,
    report_root: Path,
    *,
    compact_enabled: bool,
) -> dict:
    """创建只在 Context Compact 开关上不同的顶层初始状态。

    Args:
        input_root: 两条对比路径共享的只读输入目录。
        artifact_root: 当前路径独立使用的产物目录。
        report_root: 当前路径独立使用的报告目录。
        compact_enabled: 是否启用 Context Compact。

    Returns:
        可直接提交给顶层治理图的完整初始状态。
    """
    return create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 1.0,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": False,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(artifact_root),
            "report_root": str(report_root),
        },
        context_compact_config={
            "enabled": compact_enabled,
            "trigger_token_threshold": 1,
            "retained_preview_characters": 0,
            "persist_summaries": False,
        },
    )


def run_human_review_path(
    state: dict,
    *,
    thread_id: str,
) -> tuple[dict, dict]:
    """运行到人工暂停，再使用稳定选择恢复并完成治理。

    Args:
        state: 当前对比路径的初始顶层状态。
        thread_id: 当前图 Checkpointer 使用的隔离线程 ID。

    Returns:
        暂停状态和应用人工选择后的最终状态。
    """
    graph = build_file_governance_graph()
    config = {"configurable": {"thread_id": thread_id}}
    paused = graph.invoke(state, config=config)
    group_id = paused["human_review"]["pending_group_ids"][0]
    selected_file_id = sorted(paused["version_groups"][0]["file_ids"])[-1]
    resumed = graph.invoke(
        Command(
            resume={
                "selections": {group_id: selected_file_id},
                "review_note": "Context Compact 决策不变性测试",
            }
        ),
        config=config,
    )
    return paused, resumed


def test_compaction_preserves_version_and_human_decisions_exactly(
    tmp_path: Path,
) -> None:
    """压缩前后的版本边、分叉、推荐和人工选择必须逐值完全一致。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    create_docx(
        input_root / "proposal_v1.docx",
        "Project Alpha Amount CNY 1000 Clause A " * 20,
    )
    create_docx(
        input_root / "proposal_v2.docx",
        "Project Alpha Amount CNY 1200 Clause A " * 20,
    )
    baseline_state = create_invariance_state(
        input_root,
        tmp_path / "baseline-artifacts",
        tmp_path / "baseline-reports",
        compact_enabled=False,
    )
    compact_state = create_invariance_state(
        input_root,
        tmp_path / "compact-artifacts",
        tmp_path / "compact-reports",
        compact_enabled=True,
    )

    baseline_paused, baseline_result = run_human_review_path(
        baseline_state,
        thread_id="context-invariance-baseline",
    )
    compact_paused, compact_result = run_human_review_path(
        compact_state,
        thread_id="context-invariance-enabled",
    )

    assert baseline_paused["version_edges"] == compact_paused["version_edges"]
    assert baseline_paused["branches"] == compact_paused["branches"]
    assert baseline_paused["decisions"] == compact_paused["decisions"]
    assert (
        baseline_paused["human_review"]["pending_group_ids"]
        == compact_paused["human_review"]["pending_group_ids"]
    )
    assert baseline_result["version_edges"] == compact_result["version_edges"]
    assert baseline_result["branches"] == compact_result["branches"]
    assert baseline_result["decisions"] == compact_result["decisions"]
    assert baseline_result["human_review"] == compact_result["human_review"]
    assert compact_result["context_compact"]["summaries"]
    assert compact_result["documents"][0]["content_preview"] == ""
    assert baseline_result["documents"][0]["content_preview"] != ""
