from __future__ import annotations

from typing import cast

import pytest

from app.skills.loader import load_skill_registry_metadata
from app.skills.selector import select_skills_for_task
from app.state.models import TaskItem

"""本文件验证 Skill 选择严格受 Task 类型、固定角色和 Agent 注册表共同约束。"""


def create_task(
    task_type: str,
    role: str,
) -> TaskItem:
    """创建 Skill 选择单元测试使用的最小真实 Task。

    Args:
        task_type: 固定治理 Task 类型。
        role: Task 当前分配的固定 Agent 角色。

    Returns:
        字段完整且可传给选择器的 Task 记录。
    """
    return cast(
        TaskItem,
        {
            "task_id": f"run-skill:{task_type}",
            "task_type": task_type,
            "title": "Skill 选择测试",
            "status": "running",
            "dependencies": [],
            "assigned_role": role,
            "input_refs": [],
            "output_refs": [],
            "error": None,
            "created_at": "2026-07-23T00:00:00+00:00",
            "updated_at": "2026-07-23T00:00:00+00:00",
        },
    )


@pytest.mark.parametrize(
    ("task_type", "role", "expected_skill_ids"),
    [
        ("inventory", "content", ["file-content-analysis"]),
        ("version_analysis", "version", ["version-relation"]),
        ("evidence", "evidence", ["evidence-confidence"]),
        ("recommendation", "coordinator", ["governance-report"]),
        ("human_review", "coordinator", ["governance-report"]),
        ("report", "coordinator", ["governance-report"]),
    ],
)
def test_selector_returns_only_skills_required_by_current_task(
    task_type: str,
    role: str,
    expected_skill_ids: list[str],
) -> None:
    """每个 Task 只能选择注册表和固定职责共同声明的最小 Skill 集合。"""
    registry = load_skill_registry_metadata()

    selection = select_skills_for_task(registry, create_task(task_type, role))

    assert selection["task_type"] == task_type
    assert selection["role"] == role
    assert selection["skill_ids"] == expected_skill_ids


def test_selector_rejects_task_with_mismatched_fixed_role() -> None:
    """Subagent Task 被错误分给其他固定角色时不得选择任何 Skill。"""
    registry = load_skill_registry_metadata()

    with pytest.raises(ValueError, match="没有可用 Skill|定义不一致"):
        select_skills_for_task(
            registry,
            create_task("version_analysis", "content"),
        )
