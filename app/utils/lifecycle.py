from __future__ import annotations

from typing import cast

from app.llm.config import create_llm_config_state
from app.state.factories import (
    create_hook_config_state,
    create_prompt_state,
    create_team_state,
)
from app.state.models import FileGovernanceState

"""本模块提供生命周期节点复用的状态补齐和运行阶段更新辅助函数。"""


def with_lifecycle_defaults(state: FileGovernanceState) -> FileGovernanceState:
    """为旧 checkpoint 补齐生命周期字段并把单模型 LLM 转换为 Profile。

    Args:
        state: 可能来自 0.2.0 checkpoint 或测试夹具的顶层治理状态。

    Returns:
        包含 Prompt、Hook、多模型 Profile、Team、Task 和审计默认字段的浅复制状态。
    """
    normalized_state = dict(state)
    normalized_state.setdefault("prompt", create_prompt_state())
    normalized_state.setdefault("hooks", create_hook_config_state())
    normalized_state["llm"] = create_llm_config_state(normalized_state.get("llm"))
    normalized_state.setdefault("team", create_team_state())
    normalized_state.setdefault("hook_events", [])
    normalized_state.setdefault("tasks", [])
    normalized_state.setdefault("todos", [])
    normalized_state.setdefault("team_messages", [])
    normalized_state.setdefault("llm_calls", [])
    return cast(FileGovernanceState, normalized_state)


def update_run_stage(state: FileGovernanceState, stage: str) -> dict:
    """复制运行状态并更新当前生命周期阶段。

    Args:
        state: 包含运行状态的顶层治理状态。
        stage: 等待记录的节点执行阶段。

    Returns:
        仅修改 ``current_stage`` 的独立运行状态字典。
    """
    run = dict(state.get("run", {}))
    run["current_stage"] = stage
    return run
