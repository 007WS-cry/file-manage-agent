from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from docx import Document
from langgraph.types import Command

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state
from app.state.models import FileGovernanceState, TaskItem
from app.utils.runtime import create_error_record

"""本文件集成测试顶层业务流程、人工中断和失败报告对应的确定性 Task 进度。"""

# 顶层治理固定使用的六个 Task 顺序。
EXPECTED_TASK_ORDER = (
    "inventory",
    "version_analysis",
    "evidence",
    "recommendation",
    "human_review",
    "report",
)


def create_docx(path: Path, text: str) -> None:
    """创建 Task 进度集成测试使用的最小 DOCX 文件。

    Args:
        path: 测试文件输出路径。
        text: 写入首个正文段落的内容。
    """
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def create_progress_state(
    tmp_path: Path,
    *,
    auto_select_threshold: float,
    with_documents: bool = True,
) -> FileGovernanceState:
    """创建可走自动、人工或无数据路径的顶层测试状态。

    Args:
        tmp_path: pytest 提供的隔离临时目录。
        auto_select_threshold: 推荐进入自动选择或人工审核的阈值。
        with_documents: 是否创建两个可分析 DOCX 文件。

    Returns:
        可直接提交给顶层文件治理图的完整初始状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    if with_documents:
        create_docx(input_root / "contract_v1.docx", "Amount CNY 1000 Clause A")
        create_docx(input_root / "contract_v2.docx", "Amount CNY 1200 Clause A")
    return create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": auto_select_threshold,
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
    )


def task_by_type(state: dict[str, Any], task_type: str) -> TaskItem:
    """从图结果中返回指定类型的唯一 Task。

    Args:
        state: 顶层图返回的状态字典。
        task_type: 等待查找的固定 Task 类型。

    Returns:
        与类型匹配的 Task。

    Raises:
        AssertionError: 图结果缺少指定 Task 时抛出。
    """
    for task in state["tasks"]:
        if task["task_type"] == task_type:
            return task
    raise AssertionError(f"缺少 Task：{task_type}")


def test_automatic_path_advances_business_tasks_and_skips_human_review(
    tmp_path: Path,
) -> None:
    """自动路径应按固定顺序完成四个业务 Task、跳过审核并完成报告。"""
    state = create_progress_state(tmp_path, auto_select_threshold=0.0)

    result = build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "task-progress-automatic"}},
    )

    assert [task["task_type"] for task in result["tasks"]] == list(EXPECTED_TASK_ORDER)
    assert [task["status"] for task in result["tasks"]] == [
        "completed",
        "completed",
        "completed",
        "completed",
        "skipped",
        "completed",
    ]
    assert task_by_type(result, "human_review")["error"] is None
    assert task_by_type(result, "report")["output_refs"] == ["report"]
    assert [todo["status"] for todo in result["todos"]] == ["completed"] * 4


def test_human_review_is_running_during_interrupt_and_completed_after_resume(
    tmp_path: Path,
) -> None:
    """人工审核 Task 应跨 interrupt 保持 running，并在恢复后连同报告完成。"""
    state = create_progress_state(tmp_path, auto_select_threshold=1.0)
    graph = build_file_governance_graph()
    config = {"configurable": {"thread_id": "task-progress-human"}}

    paused = graph.invoke(state, config=config)

    assert paused["run"]["status"] == "waiting_human"
    assert paused.get("__interrupt__")
    assert task_by_type(paused, "human_review")["status"] == "running"
    assert task_by_type(paused, "report")["status"] == "pending"
    assert [task_by_type(paused, task_type)["status"] for task_type in EXPECTED_TASK_ORDER[:4]] == [
        "completed"
    ] * 4

    group_id = paused["human_review"]["pending_group_ids"][0]
    selected_file_id = paused["version_groups"][0]["file_ids"][-1]
    resumed = graph.invoke(
        Command(
            resume={
                "selections": {group_id: selected_file_id},
                "review_note": "0.3.3 Task 进度恢复测试",
            }
        ),
        config=config,
    )

    assert resumed["run"]["status"] == "completed"
    assert task_by_type(resumed, "human_review")["status"] == "completed"
    assert task_by_type(resumed, "report")["status"] == "completed"
    assert resumed["decisions"][0]["selected_by"] == "human"
    assert "选择方式：`human`" in resumed["report"]["report_markdown"]
    assert [todo["status"] for todo in resumed["todos"]] == ["completed"] * 4


def test_no_data_report_settles_all_todos_without_pending_items(
    tmp_path: Path,
) -> None:
    """无数据路径应正常跳过未执行阶段并完成报告，不留下 pending Todo。"""
    state = create_progress_state(
        tmp_path,
        auto_select_threshold=0.82,
        with_documents=False,
    )

    result = build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "task-progress-no-data"}},
    )

    assert [task["status"] for task in result["tasks"]] == [
        "completed",
        "skipped",
        "skipped",
        "skipped",
        "skipped",
        "completed",
    ]
    assert all(task["error"] is None for task in result["tasks"])
    assert all(todo["status"] == "completed" for todo in result["todos"])
    assert "未发现可用于版本分析的标准化文档" in result["report"]["report_markdown"]


def test_business_partial_fallback_continues_safe_downstream_tasks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """业务子图应用 partial_result 后应保留部分结果并继续可安全执行的下游。"""
    governance_module = importlib.import_module("app.graphs.file_governance")

    def fail_version_analysis(state: FileGovernanceState) -> dict:
        """返回确定性的 Version Analysis 致命错误以验证顶层失败同步。"""
        del state
        return {
            "errors": [
                create_error_record(
                    stage="version_analysis",
                    node_name="run_version_analysis_subgraph",
                    category="comparison",
                    message="测试注入的版本分析失败",
                    fatal=True,
                )
            ]
        }

    monkeypatch.setattr(
        governance_module,
        "run_version_analysis_subgraph",
        fail_version_analysis,
    )
    state = create_progress_state(tmp_path, auto_select_threshold=0.0)

    result = governance_module.build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "task-progress-failure"}},
    )

    assert task_by_type(result, "inventory")["status"] == "completed"
    assert task_by_type(result, "version_analysis")["status"] == "partial"
    assert task_by_type(result, "evidence")["status"] == "completed"
    assert task_by_type(result, "recommendation")["status"] == "completed"
    human_review_task = task_by_type(result, "human_review")
    assert human_review_task["status"] == "skipped"
    assert human_review_task["error"] is None
    assert sum(task["status"] == "failed" for task in result["tasks"]) == 0
    assert task_by_type(result, "report")["status"] == "completed"
    assert all(todo["status"] == "completed" for todo in result["todos"])
    assert result["run"]["status"] == "partial"
    assert any(
        error["status"] == "fallback_applied" and error["fallback"] == "partial_result"
        for error in result["errors"]
    )
