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
    route_evidence_result,
    route_version_analysis_result,
)
from app.nodes.lifecycle import finalize_run, initialize_run, validate_request
from app.nodes.report import (
    generate_failure_report,
    generate_governance_report,
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

"""本文件以 0.2.0 顶层业务路径为参照，验证关闭 Prompt 和 Hooks 后的结果兼容性。"""

# 0.2.0 与 0.3.0 需要逐项保持一致的业务结果字段。
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
    """创建兼容测试使用的最小 DOCX 业务文件。

    Args:
        path: DOCX 文件写入路径。
        text: 写入首个正文段落的测试文本。
    """
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def build_v020_reference_graph() -> Any:
    """使用当前未修改的业务节点重建 0.2.0 顶层执行路径。

    参照图不注册 Prompt 和 Hook 生命周期节点，只复现 0.2.0 的初始化、请求校验、
    四个业务子图、可选人工审核、报告和最终收口，用于隔离比较 0.3.0 新增层的影响。

    Returns:
        已编译且不带生命周期扩展节点的 0.2.0 参照 LangGraph。
    """
    builder = StateGraph(FileGovernanceState)
    builder.add_node("initialize_run", initialize_run)
    builder.add_node("validate_request", validate_request)
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
    builder.add_node("finalize_run", finalize_run)

    builder.add_edge(START, "initialize_run")
    builder.add_edge("initialize_run", "validate_request")
    builder.add_conditional_edges(
        "validate_request",
        is_request_valid,
        {"valid": "run_inventory_subgraph", "invalid": "generate_failure_report"},
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
    builder.add_edge("generate_failure_report", "finalize_run")
    builder.add_edge("generate_no_data_report", "finalize_run")
    builder.add_edge("generate_governance_report", "finalize_run")
    builder.add_edge("finalize_run", END)
    return builder.compile()


def create_compatibility_state(tmp_path: Path) -> FileGovernanceState:
    """创建显式关闭 Prompt 和 Hooks 的双版本兼容测试状态。

    Args:
        tmp_path: pytest 提供的隔离临时目录。

    Returns:
        带固定运行 ID、两个可分析 DOCX 和隔离输出目录的顶层状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    create_docx(input_root / "contract_v1.docx", "Amount CNY 1000 Clause A")
    create_docx(input_root / "contract_final.docx", "Amount CNY 1200 Clause A")
    state = create_initial_state(
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
        prompt_config={"enabled": False},
        hook_config={"enabled": False},
    )
    state["run"]["run_id"] = "v020-compatibility-reference"
    return state


def test_disabled_prompt_and_hooks_match_v020_business_results(tmp_path: Path) -> None:
    """0.3.0 完全关闭生命周期扩展时必须保持 0.2.0 的业务结果。"""
    initial_state = create_compatibility_state(tmp_path)
    reference_result = build_v020_reference_graph().invoke(deepcopy(initial_state))
    current_result = build_file_governance_graph().invoke(
        deepcopy(initial_state),
        config={"configurable": {"thread_id": "v030-disabled-compatibility"}},
    )

    assert current_result["prompt"]["status"] == "disabled"
    assert current_result["hooks"]["enabled"] is False
    assert current_result["hook_events"] == []
    assert "prompt" not in current_result["request"]
    assert "hooks" not in current_result["request"]
    assert current_result["run"]["status"] == reference_result["run"]["status"]
    for field_name in BUSINESS_RESULT_FIELDS:
        assert current_result[field_name] == reference_result[field_name]
    assert current_result["report"]["summary"] == reference_result["report"]["summary"]
    assert (
        current_result["report"]["report_markdown"]
        == reference_result["report"]["report_markdown"]
    )
    assert current_result["report"]["warnings"] == reference_result["report"]["warnings"]
