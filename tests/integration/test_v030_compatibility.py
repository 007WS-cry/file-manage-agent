from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from docx import Document
from langgraph.graph import END, START, StateGraph

from app.graphs.file_governance import build_file_governance_graph
from app.graphs.routers import (
    has_analyzable_documents,
    has_pending_human_review,
    is_request_valid,
    route_after_run_hooks_result,
    route_before_run_hooks_result,
    route_evidence_result,
    route_system_prompt_result,
    route_version_analysis_result,
)
from app.nodes.lifecycle import (
    execute_after_run_hooks,
    execute_before_run_hooks,
    finalize_run,
    initialize_run,
    load_system_prompt,
    validate_request,
)
from app.nodes.report import (
    generate_failure_report,
    generate_governance_report,
    generate_lifecycle_failure_report,
    generate_no_data_report,
)
from app.nodes.review import (
    apply_human_selection,
    prepare_human_review,
    request_human_review,
)
from app.nodes.subgraphs_nodes import (
    run_evidence_subgraph,
    run_inventory_subgraph,
    run_recommendation_subgraph,
    run_version_analysis_subgraph,
)
from app.state.factories import create_initial_state
from app.state.models import FileGovernanceState

"""本文件重建 0.3.0 顶层路径，验证 0.5.0 Agent 解释层不改变业务事实和报告。"""

# 0.3.0 与 0.5.0 必须逐项保持一致的业务结果字段。
BUSINESS_RESULT_FIELDS = (
    "files",
    "documents",
    "version_groups",
    "diffs",
    "version_edges",
    "branches",
    "version_chains",
    "pdf_exports",
    "deliveries",
    "decisions",
    "errors",
)


def create_docx(path: Path, text: str) -> None:
    """创建 0.3.0 兼容测试使用的最小 DOCX 文件。

    Args:
        path: DOCX 文件输出路径。
        text: 写入首个正文段落的测试内容。
    """
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def build_v030_reference_graph() -> Any:
    """使用现有业务和生命周期节点重建未接入 Task System 的 0.3.0 路径。

    Returns:
        包含 Prompt、Hooks、四个业务子图、人工审核和报告的参照 LangGraph。
    """
    builder = StateGraph(FileGovernanceState)
    builder.add_node("initialize_run", initialize_run)
    builder.add_node("execute_before_run_hooks", execute_before_run_hooks)
    builder.add_node("validate_request", validate_request)
    builder.add_node("load_system_prompt", load_system_prompt)
    builder.add_node("run_inventory_subgraph", run_inventory_subgraph)
    builder.add_node("run_version_analysis_subgraph", run_version_analysis_subgraph)
    builder.add_node("run_evidence_subgraph", run_evidence_subgraph)
    builder.add_node("run_recommendation_subgraph", run_recommendation_subgraph)
    builder.add_node("prepare_human_review", prepare_human_review)
    builder.add_node("request_human_review", request_human_review)
    builder.add_node("apply_human_selection", apply_human_selection)
    builder.add_node("generate_failure_report", generate_failure_report)
    builder.add_node("generate_no_data_report", generate_no_data_report)
    builder.add_node("generate_governance_report", generate_governance_report)
    builder.add_node(
        "generate_lifecycle_failure_report",
        generate_lifecycle_failure_report,
    )
    builder.add_node("execute_after_run_hooks", execute_after_run_hooks)
    builder.add_node("finalize_run", finalize_run)

    builder.add_edge(START, "initialize_run")
    builder.add_edge("initialize_run", "execute_before_run_hooks")
    builder.add_conditional_edges(
        "execute_before_run_hooks",
        route_before_run_hooks_result,
        {"continue": "validate_request", "failure": "generate_failure_report"},
    )
    builder.add_conditional_edges(
        "validate_request",
        is_request_valid,
        {"valid": "load_system_prompt", "invalid": "generate_failure_report"},
    )
    builder.add_conditional_edges(
        "load_system_prompt",
        route_system_prompt_result,
        {"continue": "run_inventory_subgraph", "failure": "generate_failure_report"},
    )
    builder.add_conditional_edges(
        "run_inventory_subgraph",
        has_analyzable_documents,
        {
            "analyzable": "run_version_analysis_subgraph",
            "empty": "generate_no_data_report",
            "failure": "generate_failure_report",
        },
    )
    builder.add_conditional_edges(
        "run_version_analysis_subgraph",
        route_version_analysis_result,
        {"success": "run_evidence_subgraph", "failure": "generate_failure_report"},
    )
    builder.add_conditional_edges(
        "run_evidence_subgraph",
        route_evidence_result,
        {"success": "run_recommendation_subgraph", "failure": "generate_failure_report"},
    )
    builder.add_conditional_edges(
        "run_recommendation_subgraph",
        has_pending_human_review,
        {
            "review": "prepare_human_review",
            "complete": "generate_governance_report",
            "failure": "generate_failure_report",
        },
    )
    builder.add_edge("prepare_human_review", "request_human_review")
    builder.add_edge("request_human_review", "apply_human_selection")
    builder.add_edge("apply_human_selection", "generate_governance_report")
    builder.add_edge("generate_failure_report", "execute_after_run_hooks")
    builder.add_edge("generate_no_data_report", "execute_after_run_hooks")
    builder.add_edge("generate_governance_report", "execute_after_run_hooks")
    builder.add_conditional_edges(
        "execute_after_run_hooks",
        route_after_run_hooks_result,
        {
            "finalize": "finalize_run",
            "failure": "generate_lifecycle_failure_report",
        },
    )
    builder.add_edge("generate_lifecycle_failure_report", "finalize_run")
    builder.add_edge("finalize_run", END)
    return builder.compile()


def create_compatibility_state(tmp_path: Path) -> FileGovernanceState:
    """创建关闭 Prompt 和 Hooks 的 0.3.0/0.3.3 双版本测试状态。

    Args:
        tmp_path: pytest 提供的隔离临时目录。

    Returns:
        带固定 run_id、两个 DOCX 和隔离输出目录的顶层初始状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    create_docx(input_root / "contract_v1.docx", "Amount CNY 1000 Clause A")
    create_docx(input_root / "contract_v2.docx", "Amount CNY 1200 Clause A")
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
        prompt_config={"enabled": False},
        hook_config={"enabled": False},
    )
    state["run"]["run_id"] = "v030-task-compatibility"
    return state


def test_task_tracking_preserves_v030_business_and_report_results(
    tmp_path: Path,
) -> None:
    """0.5.0 Task 和 Agent 扩展不得改变 0.3.0 治理结论、审核状态或报告正文。"""
    initial_state = create_compatibility_state(tmp_path)
    reference_result = build_v030_reference_graph().invoke(deepcopy(initial_state))
    current_result = build_file_governance_graph().invoke(
        deepcopy(initial_state),
        config={"configurable": {"thread_id": "v030-task-compatibility"}},
    )

    assert current_result["run"]["status"] == reference_result["run"]["status"]
    assert current_result["llm"]["enabled"] is False
    assert current_result["llm"]["provider"] == "mock"
    assert {call["agent_id"] for call in current_result["llm_calls"]} == {
        "content-subagent",
        "evidence-subagent",
    }
    assert all(diff["summary_source"] == "deterministic" for diff in current_result["diffs"])
    for field_name in BUSINESS_RESULT_FIELDS:
        assert current_result[field_name] == reference_result[field_name]
    assert current_result["human_review"] == reference_result["human_review"]
    assert current_result["report"]["summary"] == reference_result["report"]["summary"]
    assert (
        current_result["report"]["report_markdown"] == reference_result["report"]["report_markdown"]
    )
    assert current_result["report"]["warnings"] == reference_result["report"]["warnings"]
    assert [task["status"] for task in current_result["tasks"]] == [
        "completed",
        "completed",
        "completed",
        "completed",
        "skipped",
        "completed",
    ]
