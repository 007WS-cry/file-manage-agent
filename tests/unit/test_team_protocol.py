from __future__ import annotations

import pytest

from app.agents.protocol import (
    MAX_CONTENT_PREVIEW_CHARACTERS,
    TeamProtocolError,
    create_error_message,
    create_result_message,
    validate_content_subagent_input,
    validate_team_message,
)
from app.agents.registry import (
    get_fixed_subagent_registry,
    resolve_fixed_subagent_for_task,
)
from app.state.factories import create_team_state

"""本模块验证固定 Agent 注册表、最小输入信封和 Team Message 协议约束。"""

# Team Protocol 测试使用的固定受控产物引用。
ALLOWED_ARTIFACT_REF = "artifact://normalized/document-001"


def _content_payload() -> dict[str, object]:
    """创建不包含完整正文的合法 Content Subagent 输入。

    Returns:
        可安全传给 Content Subagent 的最小输入映射。
    """
    return {
        "task_id": "run-001:inventory",
        "document_id": "document-001",
        "content_preview": "合同编号 HT-001，金额字段已由确定性解析器提取。",
        "structure_summary": {"paragraphs": 5, "tables": 1},
        "key_fields": {"contract_id": "HT-001", "amount": 1000},
        "artifact_refs": [ALLOWED_ARTIFACT_REF],
    }


def test_fixed_registry_maps_only_three_subagent_tasks() -> None:
    """固定注册表应只包含 Content、Version 和 Evidence 三个角色。"""
    registry = get_fixed_subagent_registry()

    assert set(registry) == {"content", "version", "evidence"}
    assert resolve_fixed_subagent_for_task("inventory").agent_id == "content-subagent"
    assert (
        resolve_fixed_subagent_for_task("version_analysis").agent_id
        == "version-subagent"
    )
    assert resolve_fixed_subagent_for_task("evidence").agent_id == "evidence-subagent"
    with pytest.raises(ValueError, match="没有固定 Subagent"):
        resolve_fixed_subagent_for_task("report")


@pytest.mark.parametrize("forbidden_field", ["full_text", "raw_content", "正文"])
def test_content_input_rejects_protocol_fields_that_can_carry_full_text(
    forbidden_field: str,
) -> None:
    """Content 输入必须拒绝未声明的完整正文型字段。

    Args:
        forbidden_field: 当前参数化用例添加的正文型字段名称。
    """
    payload = _content_payload()
    payload[forbidden_field] = "完整正文不得进入 Subagent 输入"

    with pytest.raises(TeamProtocolError, match="协议外字段"):
        validate_content_subagent_input(payload)


def test_content_input_rejects_oversized_preview_and_nested_body() -> None:
    """短预览和结构化映射都不得被用来绕过正文输入限制。"""
    oversized = _content_payload()
    oversized["content_preview"] = "文" * (MAX_CONTENT_PREVIEW_CHARACTERS + 1)
    with pytest.raises(TeamProtocolError, match="content_preview"):
        validate_content_subagent_input(oversized)

    nested_body = _content_payload()
    nested_body["structure_summary"] = {"normalized_text": "完整正文"}
    with pytest.raises(TeamProtocolError, match="正文型字段"):
        validate_content_subagent_input(nested_body)


def test_result_and_error_messages_both_pass_team_protocol() -> None:
    """成功摘要和脱敏错误都应能表示为合法 Team Message。"""
    team = create_team_state()
    result_message = create_result_message(
        team=team,
        task_id="run-001:inventory",
        sender="content-subagent",
        summary="内容摘要已完成。",
        artifact_refs=[ALLOWED_ARTIFACT_REF],
    )
    error_message = create_error_message(
        team=team,
        task_id="run-001:inventory",
        sender="content-subagent",
        summary="内容分析失败，已返回协调 Agent。",
        error="模型调用超时",
    )

    assert validate_team_message(
        result_message,
        team=team,
        allowed_artifact_refs=[ALLOWED_ARTIFACT_REF],
    )["message_type"] == "result"
    assert validate_team_message(error_message, team=team)["message_type"] == "error"
    assert error_message["error"] == "模型调用超时"


def test_team_message_rejects_uncontrolled_reference_and_unknown_member() -> None:
    """Team Message 不得携带白名单外引用或伪造固定团队成员。"""
    team = create_team_state()
    message = create_result_message(
        team=team,
        task_id="run-001:inventory",
        sender="content-subagent",
        summary="内容摘要已完成。",
        artifact_refs=[ALLOWED_ARTIFACT_REF],
    )
    with pytest.raises(TeamProtocolError, match="未授权产物引用"):
        validate_team_message(
            message,
            team=team,
            allowed_artifact_refs=["artifact://different/ref"],
        )

    forged = dict(message)
    forged["sender"] = "dynamic-recruited-agent"
    with pytest.raises(TeamProtocolError, match="固定团队"):
        validate_team_message(forged, team=team)
