from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel

from app.agents.content import (
    CONTENT_SUBAGENT_ID,
    CONTENT_SUBAGENT_TASK_TYPES,
    build_content_subagent_prompts,
    build_deterministic_content_output,
)
from app.agents.evidence import (
    EVIDENCE_SUBAGENT_ID,
    EVIDENCE_SUBAGENT_TASK_TYPES,
    build_deterministic_evidence_output,
    build_evidence_subagent_prompts,
)
from app.agents.version import (
    VERSION_SUBAGENT_ID,
    VERSION_SUBAGENT_TASK_TYPES,
    build_deterministic_version_output,
    build_version_subagent_prompts,
)
from app.state.models import (
    ContentSubagentOutput,
    EvidenceSubagentOutput,
    VersionSubagentOutput,
)

"""本模块提供三个固定 Subagent 的不可变注册表和按角色、Task 类型解析接口。"""

# 固定 Subagent Prompt 构造器的统一调用类型。
SubagentPromptBuilder = Callable[[Any], tuple[str, str]]

# 固定 Subagent 确定性回退构造器的统一调用类型。
SubagentFallbackBuilder = Callable[[Any], BaseModel]


@dataclass(frozen=True, slots=True)
class FixedSubagentDefinition:
    """描述一个不可动态招聘或替换职责的固定 Subagent。"""

    agent_id: str
    # TeamState 中使用的稳定 Agent ID。

    role: Literal["content", "version", "evidence"]
    # Agent 的唯一固定职责。

    task_types: tuple[str, ...]
    # 允许路由到该 Agent 的 Task 类型。

    output_model: type[BaseModel]
    # LLM 结构化输出必须满足的 Pydantic 模型。

    prompt_builder: SubagentPromptBuilder
    # 根据最小输入生成系统 Prompt 和用户 Prompt 的纯函数。

    fallback_builder: SubagentFallbackBuilder
    # 模型失败时根据确定性输入生成结果的纯函数。


# 三个固定角色到不可变 Agent 定义的注册表。
FIXED_SUBAGENT_REGISTRY: Mapping[str, FixedSubagentDefinition] = MappingProxyType(
    {
        "content": FixedSubagentDefinition(
            agent_id=CONTENT_SUBAGENT_ID,
            role="content",
            task_types=CONTENT_SUBAGENT_TASK_TYPES,
            output_model=ContentSubagentOutput,
            prompt_builder=build_content_subagent_prompts,
            fallback_builder=build_deterministic_content_output,
        ),
        "version": FixedSubagentDefinition(
            agent_id=VERSION_SUBAGENT_ID,
            role="version",
            task_types=VERSION_SUBAGENT_TASK_TYPES,
            output_model=VersionSubagentOutput,
            prompt_builder=build_version_subagent_prompts,
            fallback_builder=build_deterministic_version_output,
        ),
        "evidence": FixedSubagentDefinition(
            agent_id=EVIDENCE_SUBAGENT_ID,
            role="evidence",
            task_types=EVIDENCE_SUBAGENT_TASK_TYPES,
            output_model=EvidenceSubagentOutput,
            prompt_builder=build_evidence_subagent_prompts,
            fallback_builder=build_deterministic_evidence_output,
        ),
    }
)

# Task 类型到固定 Subagent 角色的只读索引。
SUBAGENT_ROLE_BY_TASK_TYPE: Mapping[str, str] = MappingProxyType(
    {
        task_type: definition.role
        for definition in FIXED_SUBAGENT_REGISTRY.values()
        for task_type in definition.task_types
    }
)


def get_fixed_subagent_registry() -> dict[str, FixedSubagentDefinition]:
    """返回固定 Subagent 注册表的浅拷贝。

    Returns:
        角色名称到不可变 Agent 定义的新字典。
    """
    return dict(FIXED_SUBAGENT_REGISTRY)


def resolve_fixed_subagent(
    role: str,
) -> FixedSubagentDefinition:
    """按照固定角色解析 Subagent 定义。

    Args:
        role: content、version 或 evidence 角色名称。

    Returns:
        对应角色的不可变固定 Agent 定义。

    Raises:
        TypeError: 角色不是字符串时抛出。
        ValueError: 角色为空或不属于固定注册表时抛出。
    """
    if not isinstance(role, str):
        raise TypeError("Subagent role 必须是字符串")
    normalized = role.strip().casefold()
    if not normalized:
        raise ValueError("Subagent role 不得为空")
    definition = FIXED_SUBAGENT_REGISTRY.get(normalized)
    if definition is None:
        raise ValueError(f"未知固定 Subagent 角色：{normalized}")
    return definition


def resolve_fixed_subagent_for_task(
    task_type: str,
) -> FixedSubagentDefinition:
    """按照固定 Task 类型解析唯一负责的 Subagent。

    Args:
        task_type: Inventory、Version Analysis 或 Evidence Task 类型。

    Returns:
        负责该 Task 类型的固定 Agent 定义。

    Raises:
        TypeError: Task 类型不是字符串时抛出。
        ValueError: Task 类型为空、属于 coordinator 或未注册时抛出。
    """
    if not isinstance(task_type, str):
        raise TypeError("task_type 必须是字符串")
    normalized = task_type.strip()
    if not normalized:
        raise ValueError("task_type 不得为空")
    role = SUBAGENT_ROLE_BY_TASK_TYPE.get(normalized)
    if role is None:
        raise ValueError(f"Task 类型没有固定 Subagent：{normalized}")
    return resolve_fixed_subagent(role)
