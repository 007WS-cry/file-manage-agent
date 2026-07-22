from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, cast

from app.state.factories import (
    DEFAULT_MAX_PARALLEL_AGENTS,
    DEFAULT_TEAM_PROTOCOL_VERSION,
    create_team_state,
)
from app.state.models import (
    AgentMemberState,
    ErrorRecord,
    TaskItem,
    TeamMessage,
    TeamOrchestrationGraphState,
    TeamState,
)
from app.utils.runtime import create_error_record, utc_now_iso

"""本模块提供 Team Orchestration 节点使用的状态转换与错误收敛辅助能力。"""

# Task 状态允许的确定性转换；终态只能幂等保持，不能重新打开。
ALLOWED_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed", "skipped"}),
    "running": frozenset({"running", "completed", "failed", "skipped"}),
    "completed": frozenset({"completed"}),
    "failed": frozenset({"failed"}),
    "skipped": frozenset({"skipped"}),
}

# 固定团队成员 ID 到职责的唯一映射，不允许请求动态增加或替换成员。
FIXED_TEAM_ROLE_BY_ID: dict[str, str] = {
    "coordinator-agent": "coordinator",
    "content-subagent": "content",
    "version-subagent": "version",
    "evidence-subagent": "evidence",
}

# TeamState 允许保存的成员运行状态。
ALLOWED_AGENT_STATUSES = frozenset({"idle", "working", "waiting", "failed"})


def create_orchestration_error(node_name: str, error: Exception) -> ErrorRecord:
    """把 Team Orchestration 节点异常转换为结构化致命校验错误。

    Args:
        node_name: 产生异常的节点函数名称。
        error: 已捕获且不会继续向 LangGraph 外传播的异常。

    Returns:
        可由顶层和子图 ``errors`` reducer 合并的结构化错误。
    """
    return create_error_record(
        stage="team_orchestration",
        node_name=node_name,
        category="validation",
        message=str(error),
        fatal=True,
    )


def create_dispatch_error(
    node_name: str,
    error: Exception,
    *,
    fatal: bool = False,
) -> ErrorRecord:
    """把 Subagent 分派异常转换为不包含输入正文的结构化编排错误。

    Args:
        node_name: 捕获分派异常的 LangGraph 节点函数名称。
        error: 协议校验、角色选择或子图调用产生的异常。
        fatal: 是否阻断整个治理运行；协调者可回退的错误默认为 False。

    Returns:
        可由 ``errors`` reducer 合并的 Team Orchestration 错误记录。
    """
    message = str(error).strip() or type(error).__name__
    return create_error_record(
        stage="team_orchestration",
        node_name=node_name,
        category="protocol",
        message=message[:1_000],
        fatal=fatal,
    )


def normalize_fixed_team(team: TeamState | None) -> TeamState:
    """初始化或校验协调者和三个固定 Subagent 组成的团队状态。

    Args:
        team: 可选已有 TeamState；首次独立调用时可以省略。

    Returns:
        成员列表和其中可变字段均已复制的固定 TeamState。

    Raises:
        TypeError: 团队字段、成员字段或列表字段类型不正确时抛出。
        ValueError: 成员、角色、协议版本、并发上限或 Skills 违反固定契约时抛出。
    """
    if team is None:
        return create_team_state()
    if not isinstance(team, dict):
        raise TypeError("TeamState 必须是对象")
    if team.get("coordinator_id") != "coordinator-agent":
        raise ValueError("TeamState.coordinator_id 必须是 coordinator-agent")
    if team.get("protocol_version") != DEFAULT_TEAM_PROTOCOL_VERSION:
        raise ValueError(
            f"TeamState.protocol_version 必须是 {DEFAULT_TEAM_PROTOCOL_VERSION}"
        )
    max_parallel_agents = team.get("max_parallel_agents")
    if (
        isinstance(max_parallel_agents, bool)
        or not isinstance(max_parallel_agents, int)
        or not 1 <= max_parallel_agents <= DEFAULT_MAX_PARALLEL_AGENTS
    ):
        raise ValueError(
            "TeamState.max_parallel_agents 必须位于 1 到 "
            f"{DEFAULT_MAX_PARALLEL_AGENTS} 之间"
        )

    raw_members = team.get("members")
    if not isinstance(raw_members, list):
        raise TypeError("TeamState.members 必须是列表")
    if len(raw_members) != len(FIXED_TEAM_ROLE_BY_ID):
        raise ValueError("TeamState 必须且只能包含协调者和三个固定 Subagent")

    normalized_members: list[AgentMemberState] = []
    seen_ids: set[str] = set()
    for raw_member in raw_members:
        if not isinstance(raw_member, dict):
            raise TypeError("TeamState.members 的每个元素必须是对象")
        agent_id = raw_member.get("id")
        if not isinstance(agent_id, str) or agent_id not in FIXED_TEAM_ROLE_BY_ID:
            raise ValueError(f"TeamState 包含未知固定成员：{agent_id}")
        if agent_id in seen_ids:
            raise ValueError(f"TeamState 包含重复成员：{agent_id}")
        seen_ids.add(agent_id)
        expected_role = FIXED_TEAM_ROLE_BY_ID[agent_id]
        if raw_member.get("role") != expected_role:
            raise ValueError(f"成员 {agent_id} 的 role 与固定职责不一致")
        status = raw_member.get("status")
        if status not in ALLOWED_AGENT_STATUSES:
            raise ValueError(f"成员 {agent_id} 的 status 不合法")
        current_task_id = raw_member.get("current_task_id")
        if current_task_id is not None and (
            not isinstance(current_task_id, str) or not current_task_id.strip()
        ):
            raise ValueError(f"成员 {agent_id} 的 current_task_id 不合法")
        tool_names = raw_member.get("tool_names")
        skill_ids = raw_member.get("skill_ids")
        if tool_names != []:
            raise ValueError("0.4.4 固定 Subagent 不配置工具或 Worktree 能力")
        if skill_ids != []:
            raise ValueError("0.4.4 不允许固定团队提前配置 Skills")
        normalized_members.append(
            AgentMemberState(
                id=agent_id,
                role=cast(
                    Literal["coordinator", "content", "version", "evidence"],
                    expected_role,
                ),
                status=cast(
                    Literal["idle", "working", "waiting", "failed"],
                    status,
                ),
                current_task_id=current_task_id,
                tool_names=list(tool_names),
                skill_ids=[],
            )
        )
    if seen_ids != set(FIXED_TEAM_ROLE_BY_ID):
        raise ValueError("TeamState 缺少固定团队成员")
    return TeamState(
        coordinator_id="coordinator-agent",
        members=normalized_members,
        protocol_version=DEFAULT_TEAM_PROTOCOL_VERSION,
        max_parallel_agents=max_parallel_agents,
    )


def update_team_dispatch_status(
    team: TeamState,
    *,
    agent_id: str,
    task_id: str,
    active: bool,
) -> TeamState:
    """更新一次串行分派中协调者和目标 Subagent 的运行状态。

    Args:
        team: 已通过固定团队校验的 TeamState。
        agent_id: 当前被选择的固定 Subagent ID。
        task_id: 当前分派对应的真实 Task ID。
        active: True 表示开始分派，False 表示结果已收敛。

    Returns:
        不修改输入对象的新 TeamState；开始时标记 waiting/working，结束时恢复 idle。

    Raises:
        ValueError: agent_id 不是三个固定 Subagent 之一时抛出。
    """
    normalized = normalize_fixed_team(team)
    if agent_id == normalized["coordinator_id"] or agent_id not in FIXED_TEAM_ROLE_BY_ID:
        raise ValueError(f"无法把 Task 分派给 Agent：{agent_id}")
    members: list[AgentMemberState] = []
    for member in normalized["members"]:
        updated = dict(member)
        if member["id"] == normalized["coordinator_id"]:
            updated["status"] = "waiting" if active else "idle"
            updated["current_task_id"] = task_id if active else None
        elif member["id"] == agent_id:
            updated["status"] = "working" if active else "idle"
            updated["current_task_id"] = task_id if active else None
        members.append(cast(AgentMemberState, updated))
    return TeamState(
        coordinator_id=normalized["coordinator_id"],
        members=members,
        protocol_version=normalized["protocol_version"],
        max_parallel_agents=normalized["max_parallel_agents"],
    )


def find_latest_subagent_message(
    messages: Sequence[TeamMessage],
    *,
    task_id: str,
    agent_id: str,
) -> TeamMessage | None:
    """查找当前分派中固定 Subagent 最近返回的 result 或 error 消息。

    Args:
        messages: 当前 Team Orchestration 状态中的协议消息序列。
        task_id: 当前分派对应的 Task ID。
        agent_id: 当前被选择的固定 Subagent ID。

    Returns:
        按消息列表顺序找到的最后一条响应；不存在时返回 None。
    """
    return next(
        (
            cast(TeamMessage, dict(message))
            for message in reversed(messages)
            if message.get("task_id") == task_id
            and message.get("sender") == agent_id
            and message.get("message_type") in {"result", "error"}
        ),
        None,
    )


def resolve_task_creation_time(state: TeamOrchestrationGraphState) -> str:
    """为首次创建或补齐 Task 选择稳定时间。

    优先使用顶层运行开始时间；旧 checkpoint 缺少开始时间时复用已有 Task 的
    ``created_at``，只有两者都不存在时才读取当前 UTC 时间。

    Args:
        state: 当前 Team Orchestration 子图状态。

    Returns:
        新建 Task 使用的 ISO 8601 时间字符串。
    """
    started_at = state.get("run", {}).get("started_at")
    if isinstance(started_at, str) and started_at.strip():
        return started_at
    for task in state.get("tasks", []):
        created_at = task.get("created_at")
        if isinstance(created_at, str) and created_at.strip():
            return created_at
    return utc_now_iso()


def merge_task_output_refs(
    old_refs: Sequence[str],
    new_refs: Sequence[str],
) -> list[str]:
    """按首次出现顺序合并 Task 产物引用并拒绝空引用。

    Args:
        old_refs: Task 已经保存的产物引用。
        new_refs: 本次状态更新新返回的产物引用。

    Returns:
        去重且保持稳定顺序的产物引用列表。

    Raises:
        ValueError: 任意引用不是非空字符串时抛出。
    """
    merged: list[str] = []
    for reference in [*old_refs, *new_refs]:
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("Task output_refs 只能包含非空字符串引用")
        if reference not in merged:
            merged.append(reference)
    return merged


def ensure_task_dependencies_ready(
    task: TaskItem,
    tasks_by_id: dict[str, TaskItem],
) -> None:
    """确认 Task 的所有依赖满足当前阶段的启动条件。

    普通业务 Task 只接受已完成或无错误跳过的依赖。报告 Task 是治理运行的统一
    收口阶段，因此允许依赖以 completed、failed 或 skipped 任一终态结束，确保
    失败报告仍可记录真实故障，而不会把报告自身误判为失败。

    Args:
        task: 等待进入运行或完成状态的目标 Task。
        tasks_by_id: 当前完整 Task DAG 的 ID 索引。

    Raises:
        ValueError: 依赖缺失或未达到目标 Task 所需终态时抛出。
    """
    for dependency_id in task["dependencies"]:
        dependency = tasks_by_id.get(dependency_id)
        if dependency is None:
            raise ValueError(f"Task {task['task_id']} 引用了未知依赖：{dependency_id}")
        if task["task_type"] == "report":
            if dependency["status"] not in {"completed", "failed", "skipped"}:
                raise ValueError(
                    f"Task {task['task_id']} 的依赖尚未进入终态：{dependency_id}"
                )
            continue
        dependency_completed = dependency["status"] == "completed"
        dependency_skipped_normally = dependency["status"] == "skipped" and not dependency.get(
            "error"
        )
        if not dependency_completed and not dependency_skipped_normally:
            raise ValueError(f"Task {task['task_id']} 的依赖尚未就绪：{dependency_id}")
