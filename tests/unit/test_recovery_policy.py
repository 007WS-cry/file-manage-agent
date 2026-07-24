from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.services.recovery_policy import (
    RECOVERY_ERROR_CATEGORIES,
    apply_recovery_policy_to_error,
    calculate_retry_backoff,
    create_recovery_policy_state,
    recommend_recovery_action,
    resolve_category_policy,
)
from app.utils.runtime import create_error_record

"""本模块验证 0.6.1 恢复配置、错误分类、有限重试、退避和安全降级策略。"""


def test_default_yaml_matches_code_policy_snapshot() -> None:
    """默认 YAML 恢复配置必须与无参数代码默认策略完全一致。"""
    project_root = Path(__file__).resolve().parents[2]
    raw_config = yaml.safe_load(
        (project_root / "configs" / "default.yaml").read_text(encoding="utf-8")
    )

    configured = create_recovery_policy_state(raw_config["recovery"])
    built_in = create_recovery_policy_state()

    assert configured == built_in
    assert tuple(configured["category_policies"]) == RECOVERY_ERROR_CATEGORIES


def test_category_policy_uses_bounded_deterministic_backoff() -> None:
    """超时策略必须按固定倍数退避，且不得超过类别上限。"""
    policy = create_recovery_policy_state()
    timeout_policy = resolve_category_policy(policy, "timeout")

    assert timeout_policy["retryable"] is True
    assert timeout_policy["max_retries"] == 2
    assert calculate_retry_backoff(timeout_policy, 1) == 1.0
    assert calculate_retry_backoff(timeout_policy, 2) == 2.0

    with pytest.raises(ValueError, match="超过策略允许"):
        calculate_retry_backoff(timeout_policy, 3)


def test_recovery_action_prioritizes_retry_then_fallback_or_human() -> None:
    """策略动作必须依次选择剩余重试、安全降级和人工恢复。"""
    policy = create_recovery_policy_state()
    timeout_error = create_error_record(
        stage="provider",
        node_name="invoke_model",
        category="timeout",
        message="模型调用超时",
        retryable=True,
        retry_count=0,
        max_retries=2,
        status="pending",
    )
    exhausted_timeout = {
        **timeout_error,
        "retry_count": 2,
    }
    parse_error = create_error_record(
        stage="inventory",
        node_name="extract_docx_content",
        category="parse",
        message="单文件解析失败",
        status="pending",
    )
    validation_error = create_error_record(
        stage="request_validation",
        node_name="validate_request",
        category="validation",
        message="输入路径不存在",
        status="pending",
        fatal=True,
    )

    assert recommend_recovery_action(timeout_error, policy) == "retry"
    assert recommend_recovery_action(exhausted_timeout, policy) == "wait_human"
    assert recommend_recovery_action(parse_error, policy) == "fallback"
    assert recommend_recovery_action(validation_error, policy) == "wait_human"


def test_disabled_policy_does_not_schedule_recovery_action() -> None:
    """关闭恢复策略时不得根据错误类别自动安排任何动作。"""
    policy = create_recovery_policy_state({"enabled": False})
    error = create_error_record(
        stage="inventory",
        node_name="extract_pdf_content",
        category="parse",
        message="测试解析失败",
        status="pending",
    )

    assert recommend_recovery_action(error, policy) == "none"


def test_apply_policy_upgrades_legacy_error_without_losing_facts() -> None:
    """旧版错误映射补齐策略字段后必须保留 ID、阶段、节点和致命语义。"""
    policy = create_recovery_policy_state()
    legacy_error = {
        "id": "legacy-error",
        "stage": "memory",
        "node_name": "recall_long_term_memory",
        "category": "memory",
        "message": "Memory 不可用",
        "related_file_id": None,
        "fatal": False,
    }

    upgraded = apply_recovery_policy_to_error(legacy_error, policy)

    assert upgraded["id"] == "legacy-error"
    assert upgraded["stage"] == "memory"
    assert upgraded["node_name"] == "recall_long_term_memory"
    assert upgraded["retryable"] is True
    assert upgraded["max_retries"] == 1
    assert upgraded["fallback"] == "no_memory"
    assert upgraded["requires_human"] is False
    assert upgraded["status"] == "pending"
    assert upgraded["fatal"] is False


@pytest.mark.parametrize(
    ("config", "error_type", "message"),
    [
        (
            {"unknown": True},
            ValueError,
            "未知字段",
        ),
        (
            {"categories": {"mcp": {}}},
            ValueError,
            "未知错误类别",
        ),
        (
            {
                "categories": {
                    "timeout": {
                        "retryable": False,
                    }
                }
            },
            ValueError,
            "max_retries 必须为零",
        ),
        (
            {
                "default_policy": {
                    "initial_backoff_seconds": 9.0,
                    "max_backoff_seconds": 8.0,
                }
            },
            ValueError,
            "不得小于",
        ),
    ],
)
def test_invalid_recovery_config_is_rejected(
    config: dict[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    """未知字段、未知类别和矛盾重试参数必须明确失败。"""
    with pytest.raises(error_type, match=message):
        create_recovery_policy_state(config)
