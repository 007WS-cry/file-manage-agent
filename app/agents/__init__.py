from app.agents.protocol import (
    TeamProtocolError,
    create_assignment_message,
    create_error_message,
    create_result_message,
    create_team_message,
    validate_content_subagent_input,
    validate_evidence_subagent_input,
    validate_team_message,
    validate_version_subagent_input,
)
from app.agents.registry import (
    FixedSubagentDefinition,
    get_fixed_subagent_registry,
    resolve_fixed_subagent,
    resolve_fixed_subagent_for_task,
)

"""本包集中公开三个固定 Subagent、静态注册表和 Team Protocol 接口。"""

# 本包允许业务图和测试直接导入的固定 Agent 与协议公共接口。
__all__ = [
    "FixedSubagentDefinition",
    "TeamProtocolError",
    "create_assignment_message",
    "create_error_message",
    "create_result_message",
    "create_team_message",
    "get_fixed_subagent_registry",
    "resolve_fixed_subagent",
    "resolve_fixed_subagent_for_task",
    "validate_content_subagent_input",
    "validate_evidence_subagent_input",
    "validate_team_message",
    "validate_version_subagent_input",
]
