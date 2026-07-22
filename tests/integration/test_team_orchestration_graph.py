from __future__ import annotations

from copy import deepcopy
from typing import cast

from app.graphs.team_orchestration import build_team_orchestration_graph
from app.llm.config import create_llm_config_state
from app.services.task_system import build_task_id, create_task_dag
from app.state.converters import (
    file_governance_to_team_orchestration_state,
    team_orchestration_state_to_file_governance_update,
)
from app.state.factories import create_initial_state, create_team_state
from app.state.models import (
    FileGovernanceState,
    TaskItem,
    TaskStatusUpdate,
    TeamOrchestrationGraphState,
)
from app.utils.runtime import create_error_record
from app.utils.task_tracking import run_team_orchestration_subgraph

"""本模块验证独立 Team Orchestration 子图、幂等重放和顶层状态隔离边界。"""

# 集成测试统一使用的运行 ID。
RUN_ID = "run-team-orchestration-001"

# 集成测试统一使用的运行开始时间。
STARTED_AT = "2026-07-21T09:00:00+00:00"


def _create_subgraph_state(
    *,
    tasks: list[TaskItem] | None = None,
    task_update: TaskStatusUpdate | None = None,
) -> TeamOrchestrationGraphState:
    """创建可直接传给独立 Team Orchestration 子图的状态。

    Args:
        tasks: 可选已有 Task DAG；省略时从空状态开始规划。
        task_update: 本次子图调用需要消费的可选状态更新。

    Returns:
        具有固定运行信息和空 Todo、错误列表的子图状态。
    """
    return TeamOrchestrationGraphState(
        run={
            "run_id": RUN_ID,
            "status": "running",
            "current_stage": "team_orchestration",
            "started_at": STARTED_AT,
            "finished_at": None,
        },
        llm=create_llm_config_state(),
        team=create_team_state(),
        task_update=dict(task_update) if task_update is not None else None,
        dispatch_request=None,
        dispatch_result=None,
        tasks=[dict(task) for task in tasks or []],
        todos=[],
        team_messages=[],
        llm_calls=[],
        errors=[],
    )


def _create_top_level_state() -> FileGovernanceState:
    """创建用于验证顶层转换与包装节点的最小完整治理状态。

    Returns:
        run_id 和 started_at 已设置、Task 与 Todo 为空的顶层状态。
    """
    state = create_initial_state(
        {
            "root_directory": "/data/input",
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 10,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": False,
        },
        {
            "input_root": "/data/input",
            "input_readonly": True,
            "artifact_root": "/data/artifacts/content",
            "report_root": "/data/artifacts/reports",
        },
    )
    state["run"] = {
        "run_id": RUN_ID,
        "status": "running",
        "current_stage": "team_orchestration",
        "started_at": STARTED_AT,
        "finished_at": None,
    }
    return state


def _task_by_type(tasks: list[TaskItem], task_type: str) -> TaskItem:
    """从 Task 列表中查找指定类型的唯一 Task。

    Args:
        tasks: 当前完整 Task DAG。
        task_type: 等待查找的固定 Task 类型。

    Returns:
        与 task_type 匹配的 Task。

    Raises:
        AssertionError: 找不到指定 Task 时抛出。
    """
    for task in tasks:
        if task["task_type"] == task_type:
            return task
    raise AssertionError(f"没有找到 Task：{task_type}")


def test_graph_contains_task_sync_and_fixed_subagent_dispatch_nodes() -> None:
    """独立子图应同时包含原 Task 同步和三个固定 Subagent 分派节点。"""
    graph = build_team_orchestration_graph().get_graph()
    node_names = set(graph.nodes)

    expected_nodes = {
        "create_task_dag",
        "validate_task_dag",
        "assign_tasks_to_roles",
        "initialize_fixed_agent_team",
        "validate_orchestration_action",
        "update_task_status",
        "update_todos_from_tasks",
        "validate_subagent_payload",
        "create_assignment_message",
        "invoke_content_subagent_graph",
        "invoke_version_subagent_graph",
        "invoke_evidence_subagent_graph",
        "validate_team_message",
        "fallback_to_coordinator",
        "build_fallback_result_message",
        "merge_subagent_artifacts",
        "append_task_output_refs",
    }
    assert expected_nodes.issubset(node_names)
    assert not any("worktree" in name.lower() for name in node_names)


def test_subgraph_creates_valid_dag_assigns_roles_and_projects_todos() -> None:
    """首次独立调用应完成创建、校验、分配和 Todo 投影。"""
    graph = build_team_orchestration_graph()

    result = graph.invoke(_create_subgraph_state())

    assert len(result["tasks"]) == 6
    assert len({task["task_id"] for task in result["tasks"]}) == 6
    assert [task["status"] for task in result["tasks"]] == ["pending"] * 6
    assert [task["assigned_role"] for task in result["tasks"]] == [
        "content",
        "version",
        "evidence",
        "coordinator",
        "coordinator",
        "coordinator",
    ]
    assert [todo["status"] for todo in result["todos"]] == ["pending"] * 4
    assert result["task_update"] is None
    assert result["errors"] == []


def test_repeated_subgraph_invocation_does_not_duplicate_or_reset_tasks() -> None:
    """把子图结果再次作为输入时不得重复 Task、Todo 或更新时间。"""
    graph = build_team_orchestration_graph()
    first = graph.invoke(_create_subgraph_state())
    first_snapshot = deepcopy(first)

    second = graph.invoke(first)

    assert second["tasks"] == first_snapshot["tasks"]
    assert second["todos"] == first_snapshot["todos"]
    assert len(second["tasks"]) == 6
    assert len(second["todos"]) == 4
    assert second["errors"] == []


def test_subgraph_consumes_task_update_and_reprojects_todo() -> None:
    """合法 Task 更新应写入目标 Task、清空私有命令并刷新 Todo。"""
    graph = build_team_orchestration_graph()
    planned = graph.invoke(_create_subgraph_state())
    inventory_id = build_task_id(RUN_ID, "inventory")
    running_update = TaskStatusUpdate(
        task_id=inventory_id,
        status="running",
        output_refs=[],
        error=None,
        updated_at="2026-07-21T09:00:30+00:00",
    )
    planned["task_update"] = running_update

    running = graph.invoke(planned)

    assert _task_by_type(running["tasks"], "inventory")["status"] == "running"
    assert running["todos"][0]["status"] == "in_progress"
    assert running["task_update"] is None

    running["task_update"] = TaskStatusUpdate(
        task_id=inventory_id,
        status="completed",
        output_refs=["files", "documents"],
        error=None,
        updated_at="2026-07-21T09:01:00+00:00",
    )
    completed = graph.invoke(running)
    inventory = _task_by_type(completed["tasks"], "inventory")

    assert inventory["status"] == "completed"
    assert inventory["output_refs"] == ["files", "documents"]
    assert completed["todos"][0]["status"] == "completed"
    assert completed["task_update"] is None


def test_invalid_dependency_transition_records_error_without_changing_task() -> None:
    """依赖未完成时不得启动下游 Task，并应消费无效更新命令。"""
    graph = build_team_orchestration_graph()
    planned = graph.invoke(_create_subgraph_state())
    planned["task_update"] = TaskStatusUpdate(
        task_id=build_task_id(RUN_ID, "version_analysis"),
        status="running",
        output_refs=[],
        error=None,
        updated_at="2026-07-21T09:00:30+00:00",
    )

    result = graph.invoke(planned)

    assert _task_by_type(result["tasks"], "version_analysis")["status"] == "pending"
    assert result["task_update"] is None
    assert any(
        error["node_name"] == "update_task_status" and error["fatal"]
        for error in result["errors"]
    )


def test_invalid_dag_stops_before_role_assignment_and_todo_projection() -> None:
    """循环 DAG 应从校验路由直接结束，不继续生成 Todo。"""
    tasks = create_task_dag(RUN_ID, created_at=STARTED_AT)
    inventory = _task_by_type(tasks, "inventory")
    report_id = build_task_id(RUN_ID, "report")
    tasks[0] = cast(TaskItem, {**inventory, "dependencies": [report_id]})
    graph = build_team_orchestration_graph()

    result = graph.invoke(_create_subgraph_state(tasks=tasks))

    assert result["todos"] == []
    assert {error["node_name"] for error in result["errors"]} == {
        "create_task_dag",
        "validate_task_dag",
    }
    assert all(error["fatal"] for error in result["errors"])


def test_converters_do_not_leak_task_update_or_unrelated_top_errors() -> None:
    """转换器只向子图传私有命令，返回顶层时必须删除该字段。"""
    top_state = _create_top_level_state()
    top_state["errors"] = [
        create_error_record(
            stage="inventory",
            node_name="discover_input_files",
            category="filesystem",
            message="既有业务错误",
            fatal=False,
        )
    ]
    task_update = TaskStatusUpdate(
        task_id=build_task_id(RUN_ID, "inventory"),
        status="running",
        output_refs=[],
        error=None,
        updated_at="2026-07-21T09:00:30+00:00",
    )

    subgraph_state = file_governance_to_team_orchestration_state(
        top_state,
        task_update=task_update,
    )
    assert subgraph_state["task_update"] == task_update
    assert subgraph_state["errors"] == []

    result = build_team_orchestration_graph().invoke(subgraph_state)
    top_update = team_orchestration_state_to_file_governance_update(result)

    assert set(top_update) == {
        "team",
        "tasks",
        "todos",
        "team_messages",
        "llm_calls",
        "errors",
    }
    assert "task_update" not in top_update
    assert _task_by_type(top_update["tasks"], "inventory")["status"] == "running"


def test_top_level_wrapper_returns_only_public_orchestration_fields() -> None:
    """同步包装节点应独立执行子图且只返回顶层允许字段。"""
    top_state = _create_top_level_state()
    task_update = TaskStatusUpdate(
        task_id=build_task_id(RUN_ID, "inventory"),
        status="running",
        output_refs=[],
        error=None,
        updated_at="2026-07-21T09:00:30+00:00",
    )

    update = run_team_orchestration_subgraph(
        top_state,
        task_update=task_update,
    )

    assert set(update) == {
        "team",
        "tasks",
        "todos",
        "team_messages",
        "llm_calls",
        "errors",
    }
    assert len(update["tasks"]) == 6
    assert len(update["todos"]) == 4
    assert "task_update" not in update
