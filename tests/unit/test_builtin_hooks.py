from __future__ import annotations

from pathlib import Path

import pytest

from app.hooks.builtin import (
    cleanup_run_resources_hook,
    enrich_run_state_hook,
    flush_tool_audit_hook,
    initialize_tool_audit_hook,
    validate_report_result_hook,
    validate_request_envelope_hook,
)
from app.hooks.registry import DEFAULT_HOOK_REGISTRY
from app.hooks.runner import execute_after_run_hooks, execute_before_run_hooks
from app.state.factories import create_initial_state
from app.state.models import FileGovernanceState

"""本文件单元测试内置 Hook 的请求预检、状态补充、报告检查、审计和清理边界。"""


def create_builtin_state(tmp_path: Path) -> FileGovernanceState:
    """创建内置 Hook 测试使用的最小完整顶层状态。

    Args:
        tmp_path: pytest 提供的隔离临时目录。

    Returns:
        输入工作空间保持只读的顶层文件治理状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    return create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 10,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": False,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(tmp_path / "artifacts"),
            "report_root": str(tmp_path / "reports"),
        },
    )


def test_default_registry_contains_only_expected_builtin_hooks() -> None:
    """默认静态注册表必须完整包含六个受控内置 Hook。"""
    assert set(DEFAULT_HOOK_REGISTRY) == {
        "validate_request_envelope_hook",
        "enrich_run_state_hook",
        "initialize_tool_audit_hook",
        "validate_report_result_hook",
        "flush_tool_audit_hook",
        "cleanup_run_resources_hook",
    }
    assert all(callable(hook) for hook in DEFAULT_HOOK_REGISTRY.values())


def test_request_preflight_accepts_valid_readonly_envelope(tmp_path: Path) -> None:
    """合法只读请求信封应通过预检且不产生状态更新。"""
    state = create_builtin_state(tmp_path)

    result = validate_request_envelope_hook(state)

    assert result["state_update"] == {}
    assert "预检通过" in result["message"]


def test_request_preflight_rejects_non_readonly_workspace(tmp_path: Path) -> None:
    """请求未声明输入只读时必须失败，不能交给后续业务子图。"""
    state = create_builtin_state(tmp_path)
    state["workspace"]["input_readonly"] = False

    with pytest.raises(ValueError, match="必须为 True"):
        validate_request_envelope_hook(state)


def test_enrich_run_state_does_not_mutate_input_state(tmp_path: Path) -> None:
    """运行状态补充 Hook 应返回副本，不得原地修改调用方状态。"""
    state = create_builtin_state(tmp_path)
    original_stage = state["run"]["current_stage"]

    result = enrich_run_state_hook(state)

    assert state["run"]["current_stage"] == original_stage
    assert result["state_update"]["run"]["current_stage"] == "before_run_hooks"


def test_report_validation_requires_deliverable_content(tmp_path: Path) -> None:
    """报告检查应拒绝空报告并接受完整摘要、正文和时间。"""
    state = create_builtin_state(tmp_path)

    with pytest.raises(ValueError, match="报告摘要"):
        validate_report_result_hook(state)

    state["report"].update(
        {
            "summary": "完成一个版本组治理。",
            "report_markdown": "# 治理报告",
            "generated_at": "2026-07-20T12:00:00+08:00",
        }
    )
    result = validate_report_result_hook(state)

    assert result["state_update"] == {}
    assert "检查通过" in result["message"]


def test_audit_and_cleanup_hooks_do_not_modify_business_state(tmp_path: Path) -> None:
    """第二批审计与清理 Hook 只能返回说明，不能改动业务事实或文件。"""
    state = create_builtin_state(tmp_path)

    audit_start = initialize_tool_audit_hook(state)
    audit_end = flush_tool_audit_hook(state)
    cleanup = cleanup_run_resources_hook(state)

    assert audit_start["state_update"] == {}
    assert audit_end["state_update"] == {}
    assert cleanup["state_update"] == {}
    assert state["files"] == []
    assert state["workspace"]["input_readonly"] is True


def test_default_registry_executes_configured_before_and_after_plans(
    tmp_path: Path,
) -> None:
    """默认配置中的六个名称应能通过静态注册表完成两个生命周期阶段。"""
    state = create_builtin_state(tmp_path)
    state["hooks"] = {
        "enabled": True,
        "before_run": [
            "validate_request_envelope_hook",
            "enrich_run_state_hook",
            "initialize_tool_audit_hook",
        ],
        "before_model": [],
        "after_model": [],
        "after_run": [
            "validate_report_result_hook",
            "flush_tool_audit_hook",
            "cleanup_run_resources_hook",
        ],
        "default_failure_policy": "block",
        "failure_policies": {
            "initialize_tool_audit_hook": "ignore",
            "flush_tool_audit_hook": "ignore",
            "cleanup_run_resources_hook": "ignore",
        },
    }

    before_update = execute_before_run_hooks(state)
    state["run"] = before_update["run"]
    state["report"].update(
        {
            "summary": "完成一个版本组治理。",
            "report_markdown": "# 治理报告",
            "generated_at": "2026-07-20T12:00:00+08:00",
        }
    )
    after_update = execute_after_run_hooks(state)

    assert [event["status"] for event in before_update["hook_events"]] == [
        "success",
        "success",
        "success",
    ]
    assert [event["status"] for event in after_update["hook_events"]] == [
        "success",
        "success",
        "success",
    ]
    assert "errors" not in before_update
    assert "errors" not in after_update
