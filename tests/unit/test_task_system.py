from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from app.services.task_system import (
    TASK_ROLE_BY_TYPE,
    assign_tasks_to_roles,
    build_task_execution_id,
    build_task_id,
    create_task_dag,
    resolve_subagent_task,
    topologically_sort_tasks,
    update_todos_from_tasks,
    validate_task_dag,
)
from app.state.factories import create_initial_state
from app.state.models import TaskItem
from app.state.reducers import merge_by_task_id

"""本模块验证固定 Task DAG、拓扑约束、角色映射、幂等恢复和 Todo 纯投影。"""

# 单元测试统一使用的确定性运行 ID。
RUN_ID = "run-task-system-001"

# 单元测试创建初始 Task 时使用的固定 ISO 8601 时间。
CREATED_AT = "2026-07-21T08:00:00+00:00"


def _create_tasks() -> list[TaskItem]:
    """创建一份可由单个测试安全修改的固定 Task DAG。

    Returns:
        使用测试运行 ID 和固定时间创建的六个 Task。
    """
    return create_task_dag(RUN_ID, created_at=CREATED_AT)


def _replace_task(tasks: list[TaskItem], task_type: str, **updates: object) -> None:
    """在测试副本中按类型更新一个 Task 的指定字段。

    Args:
        tasks: 当前测试独占的 Task 列表。
        task_type: 等待修改的 Task 类型。
        updates: 需要覆盖到目标 Task 的字段。

    Raises:
        AssertionError: Task 列表中不存在指定类型时抛出。
    """
    for index, task in enumerate(tasks):
        if task["task_type"] == task_type:
            tasks[index] = cast(TaskItem, {**task, **updates})
            return
    raise AssertionError(f"测试 Task DAG 中不存在类型：{task_type}")


def test_initial_state_contains_empty_task_and_todo_collections() -> None:
    """顶层初始状态应显式提供空 Task 和 Todo，供后续编排子图安全写入。"""
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

    assert state["tasks"] == []
    assert state["todos"] == []


def test_same_run_id_creates_identical_stable_dag() -> None:
    """同一运行 ID 和创建时间应得到内容、顺序和依赖完全相同的 DAG。"""
    first = create_task_dag(RUN_ID, created_at=CREATED_AT)
    second = create_task_dag(RUN_ID, created_at=CREATED_AT)

    assert first == second
    assert [task["task_id"] for task in first] == [
        build_task_id(RUN_ID, "inventory"),
        build_task_id(RUN_ID, "version_analysis"),
        build_task_id(RUN_ID, "evidence"),
        build_task_id(RUN_ID, "recommendation"),
        build_task_id(RUN_ID, "human_review"),
        build_task_id(RUN_ID, "report"),
    ]
    assert [task["execution_id"] for task in first] == [
        build_task_execution_id(RUN_ID, task["task_type"]) for task in first
    ]
    assert all(task["attempt_count"] == 0 for task in first)
    assert topologically_sort_tasks(first) == first


def test_recreating_dag_preserves_existing_state_outputs_and_times() -> None:
    """幂等重建不得重置已有 Task 的执行状态、产物、错误或时间。"""
    existing = _create_tasks()
    _replace_task(
        existing,
        "inventory",
        status="completed",
        output_refs=["files", "documents"],
        error=None,
        updated_at="2026-07-21T08:01:00+00:00",
    )
    original = deepcopy(existing)

    recreated = create_task_dag(
        RUN_ID,
        created_at="2026-07-21T09:00:00+00:00",
        existing_tasks=existing,
    )

    assert recreated == original
    assert existing == original


def test_recreating_partial_dag_only_adds_missing_tasks() -> None:
    """恢复不完整 checkpoint 时只补齐缺失 Task，并保留已有记录。"""
    complete = _create_tasks()
    existing_inventory = cast(
        TaskItem,
        {
            **complete[0],
            "status": "running",
            "updated_at": "2026-07-21T08:00:30+00:00",
        },
    )

    recreated = create_task_dag(
        RUN_ID,
        created_at="2026-07-21T09:00:00+00:00",
        existing_tasks=[existing_inventory],
    )

    assert len(recreated) == 6
    assert recreated[0] == existing_inventory
    assert recreated[1]["created_at"] == "2026-07-21T09:00:00+00:00"


def test_recreating_legacy_dag_backfills_execution_contract() -> None:
    """0.6.0 Task 应补齐稳定执行 ID 和零次尝试，且不改变已有业务字段。"""
    legacy = cast(TaskItem, dict(_create_tasks()[0]))
    legacy.pop("execution_id")
    legacy.pop("attempt_count")
    legacy["status"] = "running"
    legacy["output_refs"] = ["files"]

    recreated = create_task_dag(
        RUN_ID,
        created_at=CREATED_AT,
        existing_tasks=[legacy],
    )

    assert recreated[0]["execution_id"] == build_task_execution_id(RUN_ID, "inventory")
    assert recreated[0]["attempt_count"] == 0
    assert recreated[0]["status"] == "running"
    assert recreated[0]["output_refs"] == ["files"]


def test_merge_by_task_id_updates_without_duplicates_or_reordering() -> None:
    """Task reducer 应按 task_id 覆盖更新且保持首次出现顺序。"""
    tasks = _create_tasks()
    inventory = tasks[0]
    update = {
        "task_id": inventory["task_id"],
        "status": "completed",
        "output_refs": ["files", "documents"],
        "updated_at": "2026-07-21T08:01:00+00:00",
    }

    merged = merge_by_task_id(tasks, [update])

    assert len(merged) == len(tasks)
    assert [item["task_id"] for item in merged] == [task["task_id"] for task in tasks]
    assert merged[0]["status"] == "completed"
    assert merged[0]["created_at"] == CREATED_AT
    assert merged[0]["output_refs"] == ["files", "documents"]


def test_duplicate_task_ids_are_rejected() -> None:
    """相同 task_id 出现两次时必须拒绝 DAG，而不是静默覆盖。"""
    tasks = _create_tasks()

    with pytest.raises(ValueError, match="重复 task_id"):
        validate_task_dag([*tasks, deepcopy(tasks[0])])

    with pytest.raises(ValueError, match="重复 task_id"):
        create_task_dag(
            RUN_ID,
            created_at=CREATED_AT,
            existing_tasks=[tasks[0], deepcopy(tasks[0])],
        )


def test_unknown_dependency_is_rejected() -> None:
    """引用 DAG 外 Task 的依赖必须产生明确错误。"""
    tasks = _create_tasks()
    _replace_task(tasks, "inventory", dependencies=[f"{RUN_ID}:missing"])

    with pytest.raises(ValueError, match="未知依赖"):
        validate_task_dag(tasks)


def test_self_dependency_is_rejected() -> None:
    """Task 依赖自身时必须在拓扑排序前被拒绝。"""
    tasks = _create_tasks()
    inventory_id = build_task_id(RUN_ID, "inventory")
    _replace_task(tasks, "inventory", dependencies=[inventory_id])

    with pytest.raises(ValueError, match="不得依赖自身"):
        validate_task_dag(tasks)


def test_dependency_cycle_is_rejected() -> None:
    """多个 Task 构成依赖环时必须返回循环依赖错误。"""
    tasks = _create_tasks()
    report_id = build_task_id(RUN_ID, "report")
    _replace_task(tasks, "inventory", dependencies=[report_id])

    with pytest.raises(ValueError, match="循环依赖"):
        validate_task_dag(tasks)


def test_role_assignment_uses_fixed_mapping_without_touching_runtime_fields() -> None:
    """角色分配只修正 assigned_role，不得改变状态、产物、错误和时间。"""
    tasks = _create_tasks()
    _replace_task(
        tasks,
        "inventory",
        assigned_role="coordinator",
        status="running",
        output_refs=["partial-files"],
        updated_at="2026-07-21T08:00:30+00:00",
    )
    before = deepcopy(tasks)

    assigned = assign_tasks_to_roles(tasks)

    assert tasks == before
    assert [task["assigned_role"] for task in assigned] == [
        TASK_ROLE_BY_TYPE[task["task_type"]] for task in assigned
    ]
    assert assigned[0]["status"] == "running"
    assert assigned[0]["output_refs"] == ["partial-files"]
    assert assigned[0]["updated_at"] == "2026-07-21T08:00:30+00:00"


@pytest.mark.parametrize(
    ("task_type", "expected_role"),
    [
        ("inventory", "content"),
        ("version_analysis", "version"),
        ("evidence", "evidence"),
    ],
)
def test_resolve_subagent_task_accepts_only_three_fixed_roles(
    task_type: str,
    expected_role: str,
) -> None:
    """前三类 Task 应解析为唯一实际固定角色。"""
    task = resolve_subagent_task(_create_tasks(), build_task_id(RUN_ID, task_type))

    assert task["task_type"] == task_type
    assert task["assigned_role"] == expected_role


def test_resolve_subagent_task_rejects_coordinator_and_failed_tasks() -> None:
    """协调者 Task 和已经失败的 Subagent Task 均不得再次分派。"""
    tasks = _create_tasks()

    with pytest.raises(ValueError, match="不允许分派"):
        resolve_subagent_task(tasks, build_task_id(RUN_ID, "recommendation"))

    _replace_task(tasks, "inventory", status="failed", error="扫描失败")
    with pytest.raises(ValueError, match="不允许再次分派"):
        resolve_subagent_task(tasks, build_task_id(RUN_ID, "inventory"))


def test_resolve_subagent_task_rejects_role_tampering() -> None:
    """Task assigned_role 被篡改时不得根据输入辨识字段绕过固定职责。"""
    tasks = _create_tasks()
    _replace_task(tasks, "inventory", assigned_role="version")

    with pytest.raises(ValueError, match="固定职责不一致"):
        resolve_subagent_task(tasks, build_task_id(RUN_ID, "inventory"))


def test_todos_are_a_deterministic_pure_projection_of_tasks() -> None:
    """相同 Task 状态应产生相同 Todo，且投影过程不得修改 Task。"""
    tasks = _create_tasks()
    _replace_task(tasks, "inventory", status="completed")
    _replace_task(tasks, "version_analysis", status="running")
    before = deepcopy(tasks)

    first = update_todos_from_tasks(RUN_ID, tasks)
    second = update_todos_from_tasks(RUN_ID, tasks)

    assert first == second
    assert tasks == before
    assert [todo["status"] for todo in first] == [
        "completed",
        "in_progress",
        "pending",
        "pending",
    ]
    assert [todo["order"] for todo in first] == [1, 2, 3, 4]


def test_todo_projection_distinguishes_normal_skip_from_blocked_skip() -> None:
    """正常跳过应视为完成，因失败依赖跳过则应展示为 blocked。"""
    normal_tasks = _create_tasks()
    _replace_task(normal_tasks, "human_review", status="skipped", error=None)
    normal_todos = update_todos_from_tasks(RUN_ID, normal_tasks)

    blocked_tasks = _create_tasks()
    _replace_task(
        blocked_tasks,
        "human_review",
        status="skipped",
        error="被上游 recommendation 失败阻断",
    )
    blocked_todos = update_todos_from_tasks(RUN_ID, blocked_tasks)

    assert normal_todos[2]["status"] == "completed"
    assert blocked_todos[2]["status"] == "blocked"


def test_todo_projection_maps_retrying_and_partial_statuses() -> None:
    """重试中 Task 应展示进行中，部分完成 Task 应允许关联 Todo 正常收口。"""
    retrying_tasks = _create_tasks()
    _replace_task(retrying_tasks, "inventory", status="retrying", error="准备重试")
    partial_tasks = _create_tasks()
    _replace_task(partial_tasks, "inventory", status="partial", error="跳过一个损坏文件")

    retrying_todos = update_todos_from_tasks(RUN_ID, retrying_tasks)
    partial_todos = update_todos_from_tasks(RUN_ID, partial_tasks)

    assert retrying_todos[0]["status"] == "in_progress"
    assert partial_todos[0]["status"] == "completed"


def test_failed_task_blocks_its_related_todo() -> None:
    """任一关联 Task 失败时，对应的高层 Todo 必须展示为 blocked。"""
    tasks = _create_tasks()
    _replace_task(tasks, "evidence", status="failed", error="证据状态引用无效")

    todos = update_todos_from_tasks(RUN_ID, tasks)

    assert todos[1]["status"] == "blocked"


def test_todo_projection_rejects_incomplete_fixed_dag() -> None:
    """缺少固定 Task 时不得生成看似完整的 Todo 进度。"""
    tasks = _create_tasks()

    with pytest.raises(ValueError, match="缺少 Task 类型"):
        update_todos_from_tasks(RUN_ID, tasks[:-1])
