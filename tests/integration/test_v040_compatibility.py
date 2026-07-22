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
    route_failure_report_task_sync,
    route_system_prompt_result,
    route_team_orchestration_result,
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
from app.nodes.task_tracking import (
    plan_run_tasks,
    sync_evidence_task_status,
    sync_human_review_task_status,
    sync_inventory_task_status,
    sync_recommendation_task_status,
    sync_report_task_status,
    sync_version_task_status,
)
from app.state.factories import create_initial_state
from app.state.models import FileGovernanceState

"""本文件以 0.4.0 确定性主路径为参照，验证 0.5.0 关闭 LLM 后的治理结论兼容性。"""

# 0.5.0 解释层不得修改的确定性治理结论字段。
GOVERNANCE_CONCLUSION_FIELDS = (
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

# 0.5.0 新增且 0.4.0 checkpoint 不包含的顶层状态字段。
V050_STATE_FIELDS = ("llm", "team", "team_messages", "llm_calls")


def create_docx(path: Path, text: str) -> None:
    """创建 0.4.0/0.5.0 兼容测试使用的最小 DOCX 文件。

    Args:
        path: DOCX 文件输出路径。
        text: 写入首个正文段落的测试内容。
    """
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def build_v040_reference_graph() -> Any:
    """重建不包含 Content、Version、Evidence Subagent 分派的 0.4.0 主路径。

    参照图保留 0.4.0 已有的生命周期、确定性业务子图、Task 同步、人工审核和报告，
    但不注册 0.5.0 引入的阶段后 Subagent 分派。Version Analysis 在请求关闭模型摘要时
    只执行确定性比较，因此可以隔离验证新增解释层是否改变原有治理结论。

    Returns:
        已编译且不包含三个固定 Subagent 业务分派的参照 LangGraph。
    """
    builder = StateGraph(FileGovernanceState)
    builder.add_node("initialize_run", initialize_run)
    builder.add_node("execute_before_run_hooks", execute_before_run_hooks)
    builder.add_node("validate_request", validate_request)
    builder.add_node("load_system_prompt", load_system_prompt)
    builder.add_node("plan_run_tasks", plan_run_tasks)
    builder.add_node("run_inventory_subgraph", run_inventory_subgraph)
    builder.add_node("sync_inventory_task_status", sync_inventory_task_status)
    builder.add_node("run_version_analysis_subgraph", run_version_analysis_subgraph)
    builder.add_node("sync_version_task_status", sync_version_task_status)
    builder.add_node("run_evidence_subgraph", run_evidence_subgraph)
    builder.add_node("sync_evidence_task_status", sync_evidence_task_status)
    builder.add_node("run_recommendation_subgraph", run_recommendation_subgraph)
    builder.add_node("sync_recommendation_task_status", sync_recommendation_task_status)
    builder.add_node("prepare_human_review", prepare_human_review)
    builder.add_node("request_human_review", request_human_review)
    builder.add_node("apply_human_selection", apply_human_selection)
    builder.add_node("sync_human_review_task_status", sync_human_review_task_status)
    builder.add_node("generate_failure_report", generate_failure_report)
    builder.add_node("generate_no_data_report", generate_no_data_report)
    builder.add_node("generate_governance_report", generate_governance_report)
    builder.add_node("sync_report_task_status", sync_report_task_status)
    builder.add_node("execute_after_run_hooks", execute_after_run_hooks)
    builder.add_node("generate_lifecycle_failure_report", generate_lifecycle_failure_report)
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
        {"continue": "plan_run_tasks", "failure": "generate_failure_report"},
    )
    builder.add_conditional_edges(
        "plan_run_tasks",
        route_team_orchestration_result,
        {"success": "run_inventory_subgraph", "failure": "generate_failure_report"},
    )
    builder.add_edge("run_inventory_subgraph", "sync_inventory_task_status")
    builder.add_conditional_edges(
        "sync_inventory_task_status",
        has_analyzable_documents,
        {
            "analyzable": "run_version_analysis_subgraph",
            "empty": "generate_no_data_report",
            "failure": "generate_failure_report",
        },
    )
    builder.add_edge("run_version_analysis_subgraph", "sync_version_task_status")
    builder.add_conditional_edges(
        "sync_version_task_status",
        route_version_analysis_result,
        {"success": "run_evidence_subgraph", "failure": "generate_failure_report"},
    )
    builder.add_edge("run_evidence_subgraph", "sync_evidence_task_status")
    builder.add_conditional_edges(
        "sync_evidence_task_status",
        route_evidence_result,
        {"success": "run_recommendation_subgraph", "failure": "generate_failure_report"},
    )
    builder.add_edge("run_recommendation_subgraph", "sync_recommendation_task_status")
    builder.add_conditional_edges(
        "sync_recommendation_task_status",
        has_pending_human_review,
        {
            "review": "prepare_human_review",
            "complete": "generate_governance_report",
            "failure": "generate_failure_report",
        },
    )
    builder.add_edge("prepare_human_review", "request_human_review")
    builder.add_edge("request_human_review", "apply_human_selection")
    builder.add_edge("apply_human_selection", "sync_human_review_task_status")
    builder.add_conditional_edges(
        "sync_human_review_task_status",
        route_team_orchestration_result,
        {"success": "generate_governance_report", "failure": "generate_failure_report"},
    )
    builder.add_conditional_edges(
        "generate_failure_report",
        route_failure_report_task_sync,
        {"sync": "sync_report_task_status", "skip": "execute_after_run_hooks"},
    )
    builder.add_edge("generate_no_data_report", "sync_report_task_status")
    builder.add_edge("generate_governance_report", "sync_report_task_status")
    builder.add_edge("sync_report_task_status", "execute_after_run_hooks")
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
    """创建关闭真实 LLM 且无需人工审核的 0.4.0 兼容测试状态。

    Args:
        tmp_path: pytest 提供的隔离临时目录。

    Returns:
        带两个候选合同版本、固定运行 ID 和隔离输出目录的顶层状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    create_docx(input_root / "contract_v1.docx", "合同金额 CNY 1000，共同条款 A。")
    create_docx(input_root / "contract_v2.docx", "合同金额 CNY 1200，共同条款 A。")
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
        llm_config={
            "enabled": False,
            "provider": "mock",
            "model": "mock-structured-v1",
        },
    )
    state["run"]["run_id"] = "v040-v050-compatibility"
    return state


def test_disabled_llm_preserves_v040_governance_conclusions(tmp_path: Path) -> None:
    """关闭 LLM 时 0.5.0 的确定性治理事实和报告必须与 0.4.0 参照路径一致。"""
    initial_state = create_compatibility_state(tmp_path)
    reference_result = build_v040_reference_graph().invoke(deepcopy(initial_state))
    current_result = build_file_governance_graph().invoke(
        deepcopy(initial_state),
        config={"configurable": {"thread_id": "v050-disabled-llm-compatibility"}},
    )

    assert current_result["run"]["status"] == reference_result["run"]["status"]
    for field_name in GOVERNANCE_CONCLUSION_FIELDS:
        assert current_result[field_name] == reference_result[field_name]
    assert current_result["report"]["summary"] == reference_result["report"]["summary"]
    assert (
        current_result["report"]["report_markdown"]
        == reference_result["report"]["report_markdown"]
    )
    assert all(diff["summary_source"] == "deterministic" for diff in current_result["diffs"])
    assert current_result["llm"]["enabled"] is False
    assert {call["provider"] for call in current_result["llm_calls"]} == {"mock"}


def test_v040_state_without_agent_fields_receives_safe_v050_defaults(tmp_path: Path) -> None:
    """缺少 0.5.0 Agent 字段的旧状态应补齐安全默认值并完成同样的治理结论。"""
    complete_state = create_compatibility_state(tmp_path)
    legacy_state = deepcopy(complete_state)
    for field_name in V050_STATE_FIELDS:
        legacy_state.pop(field_name, None)

    result = build_file_governance_graph().invoke(
        legacy_state,
        config={"configurable": {"thread_id": "v040-state-upgrade"}},
    )

    assert result["run"]["status"] == "completed"
    assert result["llm"]["enabled"] is False
    assert result["llm"]["provider"] == "mock"
    assert result["llm"]["api_key_env"] is None
    assert [member["role"] for member in result["team"]["members"]] == [
        "coordinator",
        "content",
        "version",
        "evidence",
    ]
    assert result["team_messages"]
    assert result["llm_calls"]
