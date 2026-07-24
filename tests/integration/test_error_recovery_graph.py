from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.types import Command

from app.graphs import file_governance as governance_module
from app.graphs.error_recovery import build_error_recovery_graph
from app.graphs.file_governance import build_file_governance_graph
from app.nodes import subgraphs_nodes
from app.nodes.subgraphs_nodes import (
    run_error_recovery_subgraph,
    run_inventory_subgraph,
)
from app.services.recovery_execution import execute_recoverable_subgraph
from app.state.converters import file_governance_to_recovery_state
from app.state.factories import create_initial_state
from app.state.models import ErrorRecord, FileGovernanceState
from app.storage.checkpoints import create_memory_checkpointer
from app.utils.runtime import create_error_record

"""本文件集成验证第七个恢复子图、顶层续跑、异常入口和成功结果幂等复用。"""


def create_recovery_test_state(
    tmp_path: Path,
    *,
    recovery_config: dict | None = None,
) -> FileGovernanceState:
    """创建不访问网络和应用数据库的最小恢复测试状态。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。
        recovery_config: 可选恢复策略覆盖。

    Returns:
        已具有稳定运行 ID、只读输入目录和可写产物目录的顶层状态。
    """
    input_root = tmp_path / "input"
    artifact_root = tmp_path / "artifacts"
    report_root = tmp_path / "reports"
    input_root.mkdir(exist_ok=True)
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
            "artifact_root": str(artifact_root),
            "report_root": str(report_root),
        },
        recovery_config=recovery_config,
        thread_id="recovery-integration-thread",
    )
    state["run"].update(
        {
            "run_id": "recovery-integration-run",
            "thread_id": "recovery-integration-thread",
            "status": "running",
            "current_stage": "test",
            "started_at": "2026-07-24T08:00:00+00:00",
        }
    )
    return state


def attach_error(
    state: FileGovernanceState,
    *,
    category: str,
    node_name: str = "run_inventory_subgraph",
    related_file_id: str | None = None,
) -> ErrorRecord:
    """创建并附加一个等待第七个子图处理的致命错误。

    Args:
        state: 等待加入错误的顶层状态。
        category: 恢复策略使用的错误类别。
        node_name: 失败的顶层包装节点名称。
        related_file_id: 可选关联文件 ID。

    Returns:
        已加入状态的完整错误记录。
    """
    error = create_error_record(
        stage="inventory",
        node_name=node_name,
        category=cast(Any, category),
        message="恢复图集成测试错误",
        related_file_id=related_file_id,
        status="pending",
        fatal=True,
    )
    state["errors"] = [error]
    return error


def test_error_recovery_is_seventh_subgraph_and_top_failures_enter_it() -> None:
    """恢复节点和两段续跑路由必须出现在独立子图及顶层图中。"""
    recovery_graph = build_error_recovery_graph().get_graph()
    recovery_nodes = set(recovery_graph.nodes)
    assert {
        "select_recovery_error",
        "inspect_reusable_execution",
        "decide_recovery_action",
        "schedule_recovery_retry",
        "apply_recovery_fallback",
        "prepare_recovery_human_input",
        "request_recovery_human_input",
        "apply_recovery_human_input",
        "mark_recovery_aborted",
        "finalize_recovery_outcome",
    } <= recovery_nodes

    top_graph = build_file_governance_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in top_graph.edges}
    assert (
        "run_error_recovery_subgraph",
        "select_resume_after_failed_stage",
    ) in edges
    assert (
        "select_resume_after_failed_stage",
        "sync_inventory_task_status",
    ) in edges
    for source in (
        "execute_before_run_hooks",
        "validate_request",
        "load_system_prompt",
        "load_skill_registry",
        "plan_run_tasks",
        "sync_inventory_task_status",
        "sync_version_task_status",
        "sync_evidence_task_status",
        "sync_recommendation_task_status",
        "sync_human_review_task_status",
        "execute_after_run_hooks",
    ):
        assert (source, "run_error_recovery_subgraph") in edges


def test_recovery_graph_schedules_bounded_retry(tmp_path: Path) -> None:
    """超时错误应按策略增加一次计数并返回固定失败节点。"""
    state = create_recovery_test_state(tmp_path)
    error = attach_error(state, category="timeout")

    result = build_error_recovery_graph().invoke(file_governance_to_recovery_state(state))
    recovered_error = next(item for item in result["errors"] if item["id"] == error["id"])

    assert result["recovery"]["action"] == "retry"
    assert result["recovery"]["resume_node"] == "run_inventory_subgraph"
    assert result["recovery"]["resume_after_node"] == "sync_inventory_task_status"
    assert result["recovery"]["retry_delay_seconds"] == 1.0
    assert recovered_error["status"] == "retrying"
    assert recovered_error["retry_count"] == 1
    assert recovered_error["fatal"] is False


def test_recovery_graph_applies_safe_file_fallback(tmp_path: Path) -> None:
    """解析错误应使用 skip_file 降级并留下可报告的降级记录。"""
    state = create_recovery_test_state(tmp_path)
    error = attach_error(
        state,
        category="parse",
        related_file_id="file-001",
    )

    result = build_error_recovery_graph().invoke(file_governance_to_recovery_state(state))
    recovered_error = next(item for item in result["errors"] if item["id"] == error["id"])

    assert result["recovery"]["action"] == "skip_file"
    assert recovered_error["status"] == "fallback_applied"
    assert recovered_error["fatal"] is False
    assert len(result["degradations"]) == 1
    assert result["degradations"][0]["action"] == "skip_file"
    assert result["degradations"][0]["affected_file_ids"] == ["file-001"]


def test_recovery_human_interrupt_uses_distinct_protocol(tmp_path: Path) -> None:
    """无自动动作的校验错误应以 error_recovery 协议暂停并可恢复终止。"""
    state = create_recovery_test_state(tmp_path)
    attach_error(state, category="validation")
    checkpointer = create_memory_checkpointer()
    graph = build_error_recovery_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "recovery-human-protocol"}}

    paused = graph.invoke(
        file_governance_to_recovery_state(state),
        config=config,
    )

    assert paused["run"]["status"] == "waiting_human"
    assert paused["recovery"]["human"]["kind"] == "error_recovery"
    assert paused["__interrupt__"][0].value["kind"] == "error_recovery"

    resumed = graph.invoke(
        Command(resume={"action": "abort", "note": "终止测试"}),
        config=config,
    )
    assert resumed["recovery"]["action"] == "abort"
    assert any(error["fatal"] for error in resumed["errors"])


def test_recovery_human_path_cannot_overlap_output_directory(tmp_path: Path) -> None:
    """人工替换输入目录不得包含受控产物或报告输出目录。"""
    state = create_recovery_test_state(tmp_path)
    attach_error(state, category="validation")
    artifact_root = Path(state["workspace"]["artifact_root"])
    artifact_root.mkdir()
    checkpointer = create_memory_checkpointer()
    graph = build_error_recovery_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "recovery-human-path-isolation"}}
    graph.invoke(file_governance_to_recovery_state(state), config=config)

    with pytest.raises(ValueError, match="artifact_root"):
        graph.invoke(
            Command(
                resume={
                    "action": "provide_path",
                    "replacement_path": str(artifact_root),
                }
            ),
            config=config,
        )


def test_unhandled_subgraph_exception_retries_through_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """子图未捕获超时只能进入 Recovery，并在有限重试成功后继续顶层流程。"""
    state = create_recovery_test_state(tmp_path)
    original_invoke = subgraphs_nodes.inventory_graph.invoke
    original_sync = governance_module.sync_inventory_task_status
    call_count = 0
    sync_count = 0

    def flaky_inventory_invoke(*args, **kwargs):
        """首次抛出超时，第二次执行真实 Inventory 子图。"""
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise TimeoutError("injected timeout")
        return original_invoke(*args, **kwargs)

    def counted_inventory_sync(state):
        """记录失败入口和成功重试后 Inventory Task 同步次数。"""
        nonlocal sync_count
        sync_count += 1
        return original_sync(state)

    monkeypatch.setattr(
        subgraphs_nodes.inventory_graph,
        "invoke",
        flaky_inventory_invoke,
    )
    monkeypatch.setattr(
        governance_module,
        "sync_inventory_task_status",
        counted_inventory_sync,
    )
    result = build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "subgraph-timeout-retry"}},
    )

    boundary_errors = [
        error for error in result["errors"] if error["node_name"] == "run_inventory_subgraph"
    ]
    assert call_count == 2
    assert sync_count == 1
    assert result["run"]["status"] == "partial"
    assert len(boundary_errors) == 1
    assert boundary_errors[0]["status"] == "recovered"
    assert boundary_errors[0]["fatal"] is False
    execution = next(
        item for item in result["node_executions"] if item["node_name"] == "run_inventory_subgraph"
    )
    assert execution["status"] == "succeeded"
    assert execution["attempt_count"] == 2


def test_successful_subgraph_result_is_reused_without_reexecution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """相同幂等键和输入摘要应从受控产物复用成功结果。"""
    state = create_recovery_test_state(tmp_path)
    original_invoke = subgraphs_nodes.inventory_graph.invoke
    call_count = 0

    def counted_inventory_invoke(*args, **kwargs):
        """记录真实 Inventory 子图调用次数。"""
        nonlocal call_count
        call_count += 1
        return original_invoke(*args, **kwargs)

    monkeypatch.setattr(
        subgraphs_nodes.inventory_graph,
        "invoke",
        counted_inventory_invoke,
    )
    first_update = run_inventory_subgraph(state)
    replay_state = cast(
        FileGovernanceState,
        {
            **state,
            "node_executions": first_update["node_executions"],
        },
    )

    second_update = run_inventory_subgraph(replay_state)

    assert call_count == 1
    assert second_update["node_executions"][0]["status"] == "reused"
    assert second_update["files"] == first_update["files"]
    assert second_update["documents"] == first_update["documents"]


def test_recovery_reuse_preserves_business_task_update(tmp_path: Path) -> None:
    """恢复入口复用成功产物时不得被恢复子图中的旧 Task 快照覆盖。"""
    state = create_recovery_test_state(tmp_path)
    task_update = {
        "task_id": "reused-business-task",
        "execution_id": "reused-business-execution",
        "task_type": "inventory",
        "title": "复用业务 Task",
        "status": "completed",
        "attempt_count": 1,
        "dependencies": [],
        "assigned_role": "coordinator",
        "input_refs": [],
        "output_refs": ["files"],
        "error": None,
        "created_at": "2026-07-24T08:00:00+00:00",
        "updated_at": "2026-07-24T08:01:00+00:00",
    }

    def invoke_with_task_update() -> dict:
        """模拟业务子图产生一个需要复用的 Task 更新。"""
        return {"tasks": [task_update]}

    def copy_task_update(result: dict) -> dict:
        """复制模拟业务子图的公开状态更新。"""
        return dict(result)

    first_update = execute_recoverable_subgraph(
        state,
        node_name="run_inventory_subgraph",
        invoke_subgraph=invoke_with_task_update,
        convert_result=copy_task_update,
    )
    replay_state = cast(
        FileGovernanceState,
        {
            **state,
            "node_executions": first_update["node_executions"],
        },
    )
    error = attach_error(replay_state, category="unknown")
    error["node_execution_id"] = first_update["node_executions"][0]["id"]

    recovery_update = run_error_recovery_subgraph(replay_state)

    reused_task = next(
        item for item in recovery_update["tasks"] if item["task_id"] == "reused-business-task"
    )
    assert reused_task["status"] == "completed"
