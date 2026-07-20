from __future__ import annotations

from pathlib import Path

import pytest

from app.hooks import HookResult
from app.hooks.registry import validate_hook_registrations
from app.hooks.runner import execute_before_run_hooks
from app.state.factories import create_initial_state
from app.state.models import FileGovernanceState

"""本文件单元测试 Hook runner 的顺序、静态解析、失败策略和状态写入边界。"""


def create_runner_state(
    tmp_path: Path,
    *,
    enabled: bool,
    before_run: list[str],
    failure_policies: dict[str, str] | None = None,
) -> FileGovernanceState:
    """创建 Hook runner 测试使用的最小完整顶层状态。

    Args:
        tmp_path: pytest 提供的隔离临时目录。
        enabled: 是否启用生命周期 Hooks。
        before_run: before_run 阶段的 Hook 执行顺序。
        failure_policies: 可选的单 Hook 失败策略覆盖。

    Returns:
        可直接提交给 ``execute_before_run_hooks`` 的顶层状态。
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
        hook_config={
            "enabled": enabled,
            "before_run": before_run,
            "before_model": [],
            "after_model": [],
            "after_run": [],
            "default_failure_policy": "block",
            "failure_policies": failure_policies or {},
        },
    )


def test_runner_executes_hooks_in_order_and_merges_allowed_updates(
    tmp_path: Path,
) -> None:
    """runner 应按配置顺序执行 Hook，并让后一个 Hook 看到前一个状态更新。"""
    calls: list[str] = []

    def first_hook(state: FileGovernanceState) -> HookResult:
        """记录第一次调用并更新运行阶段。"""
        calls.append("first_hook")
        run = dict(state["run"])
        run["current_stage"] = "first"
        return HookResult(message="first 完成", state_update={"run": run})

    def second_hook(state: FileGovernanceState) -> HookResult:
        """确认前序更新可见并写入最终运行阶段。"""
        calls.append("second_hook")
        assert state["run"]["current_stage"] == "first"
        run = dict(state["run"])
        run["current_stage"] = "second"
        return HookResult(message="second 完成", state_update={"run": run})

    state = create_runner_state(
        tmp_path,
        enabled=True,
        before_run=["first_hook", "second_hook"],
    )

    update = execute_before_run_hooks(
        state,
        registry={"first_hook": first_hook, "second_hook": second_hook},
    )

    assert calls == ["first_hook", "second_hook"]
    assert update["run"]["current_stage"] == "second"
    assert [event["hook_name"] for event in update["hook_events"]] == calls
    assert [event["sequence"] for event in update["hook_events"]] == [1, 2]
    assert all(event["status"] == "success" for event in update["hook_events"])


def test_runner_aggregates_ignore_and_block_without_skipping_cleanup(
    tmp_path: Path,
) -> None:
    """忽略和阻断失败都应留下事件，且阻断后仍继续执行后续清理。"""
    calls: list[str] = []

    def ignored_hook(state: FileGovernanceState) -> HookResult:
        """模拟采用 ignore 策略的失败 Hook。"""
        calls.append("ignored_hook")
        raise RuntimeError("可忽略审计失败")

    def blocked_hook(state: FileGovernanceState) -> HookResult:
        """模拟采用 block 策略的失败 Hook。"""
        calls.append("blocked_hook")
        raise RuntimeError("必须阻断的校验失败")

    def cleanup_hook(state: FileGovernanceState) -> HookResult:
        """证明前序失败后 runner 仍继续执行清理 Hook。"""
        calls.append("cleanup_hook")
        return HookResult(message="清理完成", state_update={})

    state = create_runner_state(
        tmp_path,
        enabled=True,
        before_run=["ignored_hook", "blocked_hook", "cleanup_hook"],
        failure_policies={"ignored_hook": "ignore"},
    )

    update = execute_before_run_hooks(
        state,
        registry={
            "ignored_hook": ignored_hook,
            "blocked_hook": blocked_hook,
            "cleanup_hook": cleanup_hook,
        },
    )

    assert calls == ["ignored_hook", "blocked_hook", "cleanup_hook"]
    assert [event["status"] for event in update["hook_events"]] == [
        "failed",
        "failed",
        "success",
    ]
    assert update["hook_events"][0]["failure_policy"] == "ignore"
    assert update["hook_events"][1]["failure_policy"] == "block"
    assert len(update["errors"]) == 1
    assert update["errors"][0]["node_name"] == "blocked_hook"
    assert update["errors"][0]["fatal"] is True


def test_runner_records_skipped_events_when_hooks_are_disabled(
    tmp_path: Path,
) -> None:
    """Hooks 关闭时不得调用函数，但应按配置顺序记录 skipped 事件。"""
    calls: list[str] = []

    def should_not_run(state: FileGovernanceState) -> HookResult:
        """如果被错误调用则记录测试可见副作用。"""
        calls.append("should_not_run")
        return HookResult(message="不应执行", state_update={})

    state = create_runner_state(
        tmp_path,
        enabled=False,
        before_run=["should_not_run"],
    )

    update = execute_before_run_hooks(
        state,
        registry={"should_not_run": should_not_run},
    )

    assert calls == []
    assert update["hook_events"][0]["status"] == "skipped"
    assert "errors" not in update


def test_runner_blocks_hook_that_attempts_to_modify_request(tmp_path: Path) -> None:
    """Hook 不得修改请求、工作空间或业务事实等受保护顶层字段。"""

    def unsafe_hook(state: FileGovernanceState) -> HookResult:
        """模拟尝试篡改治理请求的恶意 Hook。"""
        return HookResult(
            message="尝试修改请求",
            state_update={"request": {"max_files": 9999}},
        )

    state = create_runner_state(
        tmp_path,
        enabled=True,
        before_run=["unsafe_hook"],
    )

    update = execute_before_run_hooks(state, registry={"unsafe_hook": unsafe_hook})

    assert update["hook_events"][0]["status"] == "failed"
    assert "不得修改顶层字段" in update["hook_events"][0]["message"]
    assert update["errors"][0]["fatal"] is True
    assert "request" not in update


def test_registry_rejects_unregistered_import_like_name() -> None:
    """静态注册表不得把点号名称解释为可动态导入的 Python 函数。"""
    with pytest.raises(ValueError, match="未在静态注册表"):
        validate_hook_registrations(["os.system"])
