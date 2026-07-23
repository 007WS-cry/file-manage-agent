from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from app.skills.loader import load_skill_document
from app.state.models import (
    SkillInstructionState,
    SkillRecord,
    SkillRegistryState,
    TaskSkillSelectionState,
)

"""本模块提供 Skill 注册表复制、按选择加载、Task 绑定和确定性释放操作。"""


def copy_skill_registry(registry: SkillRegistryState) -> SkillRegistryState:
    """深复制 Skill 注册表中的可变记录和列表。

    Args:
        registry: 等待复制的 Skill 注册表状态。

    Returns:
        与输入没有列表或字典可变引用共享的注册表状态。
    """
    return SkillRegistryState(
        version=str(registry.get("version", "")),
        source_path=str(registry.get("source_path", "")),
        status=registry.get("status", "pending"),
        skills=[
            SkillRecord(
                **{
                    **dict(skill),
                    "task_types": list(skill.get("task_types", [])),
                    "roles": list(skill.get("roles", [])),
                }
            )
            for skill in registry.get("skills", [])
        ],
    )


def index_skill_records(
    registry: SkillRegistryState,
) -> dict[str, SkillRecord]:
    """按 Skill ID 建立独立记录索引并校验唯一性。

    Args:
        registry: 状态必须为 ``ready`` 的 Skill 注册表。

    Returns:
        Skill ID 到独立记录副本的映射。

    Raises:
        ValueError: 注册表未就绪、ID 为空或重复时抛出。
    """
    if registry.get("status") != "ready":
        raise ValueError("Skill 注册表尚未 ready")
    indexed: dict[str, SkillRecord] = {}
    for skill in registry.get("skills", []):
        skill_id = str(skill.get("skill_id", "")).strip()
        if not skill_id:
            raise ValueError("Skill 记录必须包含非空 skill_id")
        if skill_id in indexed:
            raise ValueError(f"Skill 注册表包含重复 ID：{skill_id}")
        indexed[skill_id] = cast(SkillRecord, dict(skill))
    if not indexed:
        raise ValueError("Skill 注册表不得为空")
    return indexed


def load_selected_skills(
    registry: SkillRegistryState,
    selection: TaskSkillSelectionState,
) -> SkillRegistryState:
    """只加载当前 Task 选择的 Skill，并清空所有非选择项正文。

    Args:
        registry: 已完成元数据加载的 Skill 注册表。
        selection: 当前 Subagent 分派的最小 Skill 选择。

    Returns:
        选择项为 ``loaded``、其余项为 ``available`` 的注册表副本。

    Raises:
        ValueError: 选择为空、引用未知 Skill 或已有其他 Task 绑定时抛出。
    """
    selected_ids = list(selection.get("skill_ids", []))
    if not selected_ids:
        raise ValueError("当前 Task 至少需要选择一个 Skill")
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("Skill 选择不得包含重复 ID")

    indexed = index_skill_records(registry)
    unknown_ids = [skill_id for skill_id in selected_ids if skill_id not in indexed]
    if unknown_ids:
        raise ValueError("Skill 选择引用未知 ID：" + ", ".join(unknown_ids))

    loaded_records: list[SkillRecord] = []
    for original in registry.get("skills", []):
        skill = cast(SkillRecord, dict(original))
        bound_task_id = skill.get("bound_task_id")
        if (
            skill.get("status") == "bound"
            and bound_task_id != selection.get("task_id")
        ):
            raise ValueError(
                f"Skill {skill['skill_id']} 已绑定到其他 Task：{bound_task_id}"
            )
        skill.update(
            {
                "status": "available",
                "bound_task_id": None,
                "content": "",
                "content_sha256": None,
            }
        )
        if skill["skill_id"] in selected_ids:
            skill = load_skill_document(
                skill,
                registry_source_path=registry["source_path"],
            )
        loaded_records.append(skill)

    loaded_order = [
        next(item for item in loaded_records if item["skill_id"] == skill_id)
        for skill_id in selected_ids
    ]
    available_order = [
        item for item in loaded_records if item["skill_id"] not in selected_ids
    ]
    updated_registry = copy_skill_registry(registry)
    updated_registry["skills"] = [*loaded_order, *available_order]
    return updated_registry


def bind_selected_skills(
    registry: SkillRegistryState,
    selection: TaskSkillSelectionState,
) -> tuple[SkillRegistryState, list[SkillInstructionState]]:
    """把已加载 Skill 绑定到当前 Task 并生成最小指令上下文。

    Args:
        registry: 已由 ``load_selected_skills`` 处理的注册表。
        selection: 当前分派的 Task、角色和 Skill ID。

    Returns:
        Skill 状态为 ``bound`` 的注册表及按选择顺序排列的指令快照。

    Raises:
        ValueError: Skill 未加载、范围不匹配、摘要缺失或选择引用未知项时抛出。
    """
    task_id = str(selection.get("task_id", "")).strip()
    task_type = str(selection.get("task_type", "")).strip()
    role = str(selection.get("role", "")).strip()
    if not task_id or not task_type or not role:
        raise ValueError("Skill 绑定必须包含非空 Task ID、Task 类型和角色")

    indexed = index_skill_records(registry)
    context: list[SkillInstructionState] = []
    for skill_id in selection.get("skill_ids", []):
        skill = indexed.get(skill_id)
        if skill is None:
            raise ValueError(f"Skill 绑定引用未知 ID：{skill_id}")
        if skill.get("status") != "loaded":
            raise ValueError(f"Skill {skill_id} 尚未按需加载")
        if task_type not in skill.get("task_types", []):
            raise ValueError(f"Skill {skill_id} 不允许用于 Task 类型 {task_type}")
        if role not in skill.get("roles", []):
            raise ValueError(f"Skill {skill_id} 不允许绑定到角色 {role}")
        content = str(skill.get("content", ""))
        digest = skill.get("content_sha256")
        if not content.strip() or not isinstance(digest, str) or not digest:
            raise ValueError(f"Skill {skill_id} 缺少已验证正文或摘要")
        context.append(
            SkillInstructionState(
                skill_id=skill_id,
                name=skill["name"],
                description=skill["description"],
                content=content,
                content_sha256=digest,
            )
        )

    selected_ids = set(selection.get("skill_ids", []))
    bound_records: list[SkillRecord] = []
    for original in registry.get("skills", []):
        skill = cast(SkillRecord, dict(original))
        if skill["skill_id"] in selected_ids:
            skill["status"] = "bound"
            skill["bound_task_id"] = task_id
        bound_records.append(skill)
    updated_registry = copy_skill_registry(registry)
    updated_registry["skills"] = bound_records
    return updated_registry, context


def release_task_skills(
    registry: SkillRegistryState,
    *,
    task_id: str | None = None,
) -> SkillRegistryState:
    """释放一个 Task 或本次编排留下的全部临时 Skill 内容。

    Args:
        registry: 包含可选 ``loaded`` 或 ``bound`` Skill 的注册表。
        task_id: 可选目标 Task ID；省略时释放全部临时加载项。

    Returns:
        目标 Skill 恢复 ``available`` 且正文、摘要和绑定 ID 已清空的注册表。
    """
    released = copy_skill_registry(registry)
    normalized_task_id = task_id.strip() if isinstance(task_id, str) else None
    records: list[SkillRecord] = []
    for original in released.get("skills", []):
        skill = cast(SkillRecord, dict(original))
        should_release = skill.get("status") == "loaded" or (
            skill.get("status") == "bound"
            and (
                normalized_task_id is None
                or skill.get("bound_task_id") == normalized_task_id
            )
        )
        if should_release:
            skill.update(
                {
                    "status": "available",
                    "bound_task_id": None,
                    "content": "",
                    "content_sha256": None,
                }
            )
        records.append(skill)
    released["skills"] = records
    return released


def skill_ids_from_context(
    context: Sequence[SkillInstructionState],
) -> list[str]:
    """从指令上下文提取顺序稳定且无重复的 Skill ID。

    Args:
        context: 已绑定到同一 Task 的 Skill 指令快照。

    Returns:
        按上下文顺序排列的唯一 Skill ID。
    """
    skill_ids: list[str] = []
    for instruction in context:
        skill_id = instruction["skill_id"]
        if skill_id not in skill_ids:
            skill_ids.append(skill_id)
    return skill_ids
