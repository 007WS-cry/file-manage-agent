from __future__ import annotations

from app.agents.registry import resolve_fixed_subagent_for_task
from app.skills.registry import index_skill_records
from app.state.models import (
    SkillRegistryState,
    TaskItem,
    TaskSkillSelectionState,
)

"""本模块根据真实 Task、固定 Agent 职责和注册表白名单选择最小 Skill 集合。"""


def select_skills_for_task(
    registry: SkillRegistryState,
    task: TaskItem,
) -> TaskSkillSelectionState:
    """为一个真实 Task 选择且仅选择其固定职责所需的 Skill。

    Subagent Task 还会与固定 Agent 注册表中的 ``skill_ids`` 交叉校验，防止
    registry.yaml 单方面扩大模型能力；Coordinator Task 则完全依据注册表中的
    Task 类型和角色白名单选择。

    Args:
        registry: 已加载元数据且状态为 ``ready`` 的 Skill 注册表。
        task: Team Orchestration 已校验并分配角色的真实 Task。

    Returns:
        包含 Task、角色和最小 Skill ID 列表的选择状态。

    Raises:
        ValueError: Task 字段为空、没有匹配 Skill 或固定 Agent 声明不一致时抛出。
    """
    task_id = str(task.get("task_id", "")).strip()
    task_type = str(task.get("task_type", "")).strip()
    role = str(task.get("assigned_role", "")).strip()
    if not task_id or not task_type or not role:
        raise ValueError("Task Skill 选择需要非空 task_id、task_type 和 assigned_role")

    indexed = index_skill_records(registry)
    matching_ids = [
        skill["skill_id"]
        for skill in registry.get("skills", [])
        if task_type in skill.get("task_types", [])
        and role in skill.get("roles", [])
    ]
    if role != "coordinator":
        definition = resolve_fixed_subagent_for_task(task_type)
        expected_ids = list(definition.skill_ids)
        missing_ids = [skill_id for skill_id in expected_ids if skill_id not in indexed]
        if missing_ids:
            raise ValueError(
                f"固定 {role} Agent 声明了未注册 Skill：" + ", ".join(missing_ids)
            )
        if matching_ids != expected_ids:
            raise ValueError(
                f"Task {task_id} 的注册表 Skill 与固定 {role} Agent 定义不一致"
            )
        selected_ids = expected_ids
    else:
        selected_ids = matching_ids

    if not selected_ids:
        raise ValueError(f"Task {task_id} 没有可用 Skill")
    return TaskSkillSelectionState(
        task_id=task_id,
        task_type=task_type,
        role=role,
        skill_ids=selected_ids,
    )
