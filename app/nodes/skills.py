from __future__ import annotations

from typing import cast

from app.services.task_system import resolve_subagent_task
from app.skills.loader import (
    create_pending_skill_registry,
    load_skill_registry_metadata,
)
from app.skills.registry import (
    bind_selected_skills,
    load_selected_skills,
    skill_ids_from_context,
)
from app.skills.registry import (
    release_task_skills as release_registry_task_skills,
)
from app.skills.selector import select_skills_for_task
from app.state.models import (
    AgentMemberState,
    FileGovernanceState,
    SkillRegistryState,
    TeamOrchestrationGraphState,
    TeamState,
)
from app.utils.runtime import create_error_record

"""本模块只定义顶层图和 Team Orchestration 图中显式注册的 Skill 生命周期节点。"""


def load_skill_registry(state: FileGovernanceState) -> dict:
    """在顶层图中只加载 Skill 元数据，不读取任何 SKILL.md 正文。

    Args:
        state: 已完成请求、Prompt 和生命周期前置校验的顶层治理状态。

    Returns:
        全部记录均为 ``available`` 的注册表；失败时返回致命校验错误。
    """
    configured = state.get("skill_registry")
    source_path = (
        configured.get("source_path")
        if isinstance(configured, dict)
        else create_pending_skill_registry()["source_path"]
    )
    try:
        return {"skill_registry": load_skill_registry_metadata(source_path)}
    except (OSError, TypeError, ValueError) as error:
        failed_registry = create_pending_skill_registry(source_path)
        failed_registry["status"] = "failed"
        return {
            "skill_registry": failed_registry,
            "errors": [
                create_error_record(
                    stage="skills",
                    node_name="load_skill_registry",
                    category="validation",
                    message=str(error),
                    fatal=True,
                )
            ],
        }


def select_task_skills(state: TeamOrchestrationGraphState) -> dict:
    """为当前 Subagent 分派解析真实 Task 并选择最小 Skill 集合。

    直接调用 Team Orchestration 且未经过顶层图时，本节点只补载默认注册表元数据，
    保持子图可独立测试；它仍不会读取任何 SKILL.md 正文。

    Args:
        state: 已完成 Task DAG、固定角色和分派动作校验的团队编排状态。

    Returns:
        就绪注册表和 Task Skill 选择；失败时清空选择并返回可回退错误。
    """
    try:
        registry = state.get("skill_registry")
        if not isinstance(registry, dict) or registry.get("status") != "ready":
            configured_path = (
                registry.get("source_path")
                if isinstance(registry, dict)
                else None
            )
            registry = load_skill_registry_metadata(configured_path)
        request = state.get("dispatch_request")
        if request is None:
            raise ValueError("Skill 选择前缺少 dispatch_request")
        task = resolve_subagent_task(state.get("tasks", []), request["task_id"])
        selection = select_skills_for_task(registry, task)
        return {
            "skill_registry": registry,
            "skill_selection": selection,
            "skill_context": [],
        }
    except (OSError, KeyError, TypeError, ValueError) as error:
        return {
            "skill_selection": None,
            "skill_context": [],
            "errors": [
                create_error_record(
                    stage="team_orchestration",
                    node_name="select_task_skills",
                    category="validation",
                    message=str(error),
                    fatal=False,
                )
            ],
        }


def load_task_skills(state: TeamOrchestrationGraphState) -> dict:
    """只读取当前 Task 已选择的 SKILL.md，清空全部非选择项正文。

    Args:
        state: 已生成 ``skill_selection`` 且注册表元数据就绪的编排状态。

    Returns:
        选择项为 ``loaded`` 的注册表；失败时返回可由协调者处理的错误。
    """
    try:
        selection = state.get("skill_selection")
        registry = state.get("skill_registry")
        if selection is None or not isinstance(registry, dict):
            raise ValueError("加载 Task Skill 前缺少选择或注册表")
        loaded = load_selected_skills(registry, selection)
        return {"skill_registry": loaded, "skill_context": []}
    except (OSError, KeyError, TypeError, ValueError) as error:
        return {
            "skill_context": [],
            "errors": [
                create_error_record(
                    stage="team_orchestration",
                    node_name="load_task_skills",
                    category="validation",
                    message=str(error),
                    fatal=False,
                )
            ],
        }


def bind_task_skills(state: TeamOrchestrationGraphState) -> dict:
    """把已加载 Skill 绑定到当前 Task 和固定 Agent。

    Args:
        state: 当前选择项已处于 ``loaded`` 状态的团队编排状态。

    Returns:
        绑定后的注册表、最小指令上下文和更新 Skill ID 的 TeamState。
    """
    try:
        selection = state.get("skill_selection")
        registry = state.get("skill_registry")
        if selection is None or not isinstance(registry, dict):
            raise ValueError("绑定 Task Skill 前缺少选择或注册表")
        bound_registry, context = bind_selected_skills(registry, selection)
        selected_ids = skill_ids_from_context(context)
        updated_members: list[AgentMemberState] = []
        matched_role = False
        for original in state["team"].get("members", []):
            member = cast(AgentMemberState, dict(original))
            if member.get("role") == selection["role"]:
                member["skill_ids"] = selected_ids
                matched_role = True
            updated_members.append(member)
        if not matched_role:
            raise ValueError(f"固定 Team 缺少角色：{selection['role']}")
        updated_team = TeamState(
            coordinator_id=state["team"]["coordinator_id"],
            members=updated_members,
            protocol_version=state["team"]["protocol_version"],
            max_parallel_agents=state["team"]["max_parallel_agents"],
        )
        return {
            "skill_registry": bound_registry,
            "skill_context": context,
            "team": updated_team,
        }
    except (KeyError, TypeError, ValueError) as error:
        return {
            "skill_context": [],
            "errors": [
                create_error_record(
                    stage="team_orchestration",
                    node_name="bind_task_skills",
                    category="validation",
                    message=str(error),
                    fatal=False,
                )
            ],
        }


def release_task_skills(state: TeamOrchestrationGraphState) -> dict:
    """在一次 Task 分派收口后释放正文并恢复 Skill 为 ``available``。

    Args:
        state: 已合并 Subagent 或协调者回退结果的团队编排状态。

    Returns:
        不再保留正文、摘要、绑定 ID 或 Agent Skill ID 的干净状态。
    """
    registry = state.get("skill_registry")
    selection = state.get("skill_selection")
    task_id = selection.get("task_id") if isinstance(selection, dict) else None
    if isinstance(registry, dict):
        released_registry = release_registry_task_skills(
            cast(SkillRegistryState, registry),
            task_id=task_id,
        )
    else:
        released_registry = create_pending_skill_registry()
    members = [
        cast(AgentMemberState, {**dict(member), "skill_ids": []})
        for member in state["team"].get("members", [])
    ]
    released_team = TeamState(
        coordinator_id=state["team"]["coordinator_id"],
        members=members,
        protocol_version=state["team"]["protocol_version"],
        max_parallel_agents=state["team"]["max_parallel_agents"],
    )
    return {
        "skill_registry": released_registry,
        "skill_selection": None,
        "skill_context": [],
        "team": released_team,
    }
