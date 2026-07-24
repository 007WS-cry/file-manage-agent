from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.entrypoints.cli import resolve_lifecycle_payload
from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state

"""本文件集成测试 Prompt、生命周期 Hooks、顶层路由和 CLI 请求信封边界。"""


def create_empty_governance_state(
    tmp_path: Path,
    *,
    prompt_config: Mapping[str, object] | None = None,
    hook_config: Mapping[str, object] | None = None,
    recovery_config: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """创建仅包含空输入目录的生命周期集成测试状态。

    Args:
        tmp_path: pytest 为当前测试提供的隔离临时目录。
        prompt_config: 可选 System Prompt 配置。
        hook_config: 可选生命周期 Hook 配置。
        recovery_config: 可选恢复策略；旧失败路径测试可显式关闭恢复。

    Returns:
        可直接提交给顶层文件治理图的完整初始状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    return create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
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
        prompt_config=prompt_config,
        hook_config=hook_config,
        recovery_config=recovery_config,
    )


def invoke_lifecycle_graph(state: dict[str, Any], *, thread_id: str) -> dict[str, Any]:
    """使用独立内存 checkpoint 调用一次顶层生命周期图。

    Args:
        state: 等待执行的顶层治理初始状态。
        thread_id: 当前测试使用的唯一 LangGraph 线程 ID。

    Returns:
        顶层治理图执行完成后的状态。
    """
    return build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": thread_id}},
    )


def test_enabled_prompt_and_builtin_hooks_run_in_configured_order(
    tmp_path: Path,
) -> None:
    """启用配置应加载 Prompt，并按顺序执行前后各三个内置 Hook。"""
    prompt_path = tmp_path / "system_prompt.md"
    prompt_path.write_text("你是文件版本治理助手。", encoding="utf-8")
    state = create_empty_governance_state(
        tmp_path,
        prompt_config={
            "enabled": True,
            "version": "integration-v1",
            "source_path": str(prompt_path),
            "dynamic_rules": ["原始业务文件必须保持只读。"],
        },
        hook_config={
            "enabled": True,
            "before_run": [
                "validate_request_envelope_hook",
                "enrich_run_state_hook",
                "initialize_tool_audit_hook",
            ],
            "after_run": [
                "validate_report_result_hook",
                "flush_tool_audit_hook",
                "cleanup_run_resources_hook",
            ],
            "default_failure_policy": "block",
        },
    )

    result = invoke_lifecycle_graph(state, thread_id="enabled-lifecycle")

    assert result["run"]["status"] == "completed"
    assert result["prompt"]["status"] == "loaded"
    assert result["prompt"]["content_sha256"] is not None
    assert "## 本次运行动态规则" in result["prompt"]["content"]
    assert [event["phase"] for event in result["hook_events"]] == [
        "before_run",
        "before_run",
        "before_run",
        "after_run",
        "after_run",
        "after_run",
    ]
    assert [event["sequence"] for event in result["hook_events"]] == [1, 2, 3, 1, 2, 3]
    assert all(event["status"] == "success" for event in result["hook_events"])


def test_missing_lifecycle_fields_use_fully_disabled_compatibility_defaults(
    tmp_path: Path,
) -> None:
    """旧版状态缺少新字段时应自动补齐关闭配置并保持原业务行为。"""
    state = create_empty_governance_state(tmp_path)
    state.pop("prompt")
    state.pop("hooks")
    state.pop("hook_events")
    state.pop("tasks")
    state.pop("todos")

    result = invoke_lifecycle_graph(state, thread_id="legacy-state-defaults")

    assert result["run"]["status"] == "completed"
    assert result["prompt"]["status"] == "disabled"
    assert result["hooks"]["enabled"] is False
    assert result["hook_events"] == []
    assert len(result["tasks"]) == 6
    assert all(todo["status"] == "completed" for todo in result["todos"])
    assert not any(error["category"] in {"prompt", "hook"} for error in result["errors"])


def test_prompt_load_failure_stops_before_inventory_and_generates_report(
    tmp_path: Path,
) -> None:
    """Prompt 文件不存在时应在扫描前失败并生成结构化失败报告。"""
    state = create_empty_governance_state(
        tmp_path,
        prompt_config={
            "enabled": True,
            "source_path": str(tmp_path / "missing_prompt.md"),
        },
        recovery_config={"enabled": False},
    )

    result = invoke_lifecycle_graph(state, thread_id="prompt-load-failure")

    assert result["run"]["status"] == "failed"
    assert result["prompt"]["status"] == "failed"
    assert result["files"] == []
    assert result["tasks"] == []
    assert result["todos"] == []
    assert any(error["category"] == "prompt" and error["fatal"] for error in result["errors"])
    assert result["report"]["summary"] == "文件版本治理未能安全完成。"


def test_blocking_before_run_hook_stops_business_graph(
    tmp_path: Path,
) -> None:
    """未注册 before_run Hook 使用 block 策略时应在请求校验前阻断。"""
    state = create_empty_governance_state(
        tmp_path,
        hook_config={
            "enabled": True,
            "before_run": ["unregistered_before_hook"],
            "default_failure_policy": "block",
        },
        recovery_config={"enabled": False},
    )

    result = invoke_lifecycle_graph(state, thread_id="blocking-before-hook")

    assert result["run"]["status"] == "failed"
    assert result["files"] == []
    assert result["tasks"] == []
    assert result["todos"] == []
    assert result["hook_events"][0]["status"] == "failed"
    assert result["hook_events"][0]["failure_policy"] == "block"
    assert any(
        error["category"] == "hook" and error["stage"] == "before_run" for error in result["errors"]
    )


def test_ignored_after_run_hook_failure_preserves_completed_result(
    tmp_path: Path,
) -> None:
    """after_run Hook 的 ignore 失败只应保留事件，不改变已完成业务结果。"""
    state = create_empty_governance_state(
        tmp_path,
        hook_config={
            "enabled": True,
            "after_run": ["unregistered_after_hook"],
            "default_failure_policy": "block",
            "failure_policies": {"unregistered_after_hook": "ignore"},
        },
    )

    result = invoke_lifecycle_graph(state, thread_id="ignored-after-hook")

    assert result["run"]["status"] == "completed"
    assert result["hook_events"][0]["status"] == "failed"
    assert result["hook_events"][0]["failure_policy"] == "ignore"
    assert not any(error["category"] == "hook" for error in result["errors"])
    assert "## 生命周期收口失败" not in result["report"]["report_markdown"]


def test_blocking_after_run_hook_generates_lifecycle_failure_report(
    tmp_path: Path,
) -> None:
    """after_run Hook 的 block 失败应保留业务报告并追加生命周期失败章节。"""
    state = create_empty_governance_state(
        tmp_path,
        hook_config={
            "enabled": True,
            "after_run": ["unregistered_after_hook"],
            "default_failure_policy": "block",
        },
        recovery_config={"enabled": False},
    )

    result = invoke_lifecycle_graph(state, thread_id="blocking-after-hook")

    assert result["run"]["status"] == "failed"
    assert result["report"]["summary"] == "业务治理结果已生成，但生命周期收口失败。"
    assert result["tasks"][-1]["task_type"] == "report"
    assert result["tasks"][-1]["status"] == "completed"
    assert "未发现可用于版本分析的标准化文档" in result["report"]["report_markdown"]
    assert "## 生命周期收口失败" in result["report"]["report_markdown"]
    assert any(
        error["category"] == "hook" and error["stage"] == "after_run" for error in result["errors"]
    )


def test_cli_lifecycle_envelope_is_resolved_separately_from_business_request(
    tmp_path: Path,
) -> None:
    """CLI 应单独解析生命周期对象和 Prompt 相对路径，不污染业务请求。"""
    payload = {
        "request": {"root_directory": "input"},
        "prompt": {
            "enabled": True,
            "source_path": "prompts/system.md",
        },
        "hooks": {
            "enabled": False,
            "before_run": ["validate_request_envelope_hook"],
        },
    }

    prompt_config, hook_config = resolve_lifecycle_payload(
        payload,
        base_directory=tmp_path,
    )

    assert prompt_config is not None
    assert prompt_config["source_path"] == str((tmp_path / "prompts/system.md").resolve())
    assert hook_config == {
        "enabled": False,
        "before_run": ["validate_request_envelope_hook"],
    }
    assert "prompt" not in payload["request"]
    assert "hooks" not in payload["request"]
