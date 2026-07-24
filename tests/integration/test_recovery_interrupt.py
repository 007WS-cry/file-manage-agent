from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from langgraph.types import Command

from app.graphs.error_recovery import build_error_recovery_graph
from app.state.converters import file_governance_to_recovery_state
from app.state.factories import create_initial_state
from app.state.models import ErrorRecord, FileGovernanceState
from app.storage.checkpoints import create_memory_checkpointer
from app.utils.runtime import create_error_record

"""本文件集成验证恢复型人工确认的四种动作、路径更新和独立 interrupt 协议。"""


def create_human_recovery_state(tmp_path: Path) -> FileGovernanceState:
    """创建自动重试已耗尽且允许四种人工恢复动作的状态。

    Args:
        tmp_path: pytest 为当前测试提供的隔离目录。

    Returns:
        包含一个关联文件校验错误的完整顶层治理状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    state = create_initial_state(
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
        recovery_config={
            "categories": {
                "validation": {
                    "retryable": True,
                    "max_retries": 1,
                }
            }
        },
        thread_id="human-recovery-thread",
    )
    state["run"].update(
        {
            "run_id": "human-recovery-run",
            "thread_id": "human-recovery-thread",
            "status": "running",
            "current_stage": "inventory",
            "started_at": "2026-07-24T08:00:00+00:00",
        }
    )
    error = create_error_record(
        stage="inventory",
        node_name="run_inventory_subgraph",
        category="validation",
        message="人工恢复集成测试错误",
        related_file_id="file-001",
        retryable=True,
        retry_count=1,
        max_retries=1,
        requires_human=True,
        status="pending",
        fatal=True,
    )
    state["errors"] = [cast(ErrorRecord, error)]
    return state


def pause_human_recovery(
    state: FileGovernanceState,
    *,
    thread_id: str,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """执行恢复子图直到人工暂停点。

    Args:
        state: 自动恢复已耗尽的顶层状态。
        thread_id: 当前测试使用的内存 checkpoint 线程 ID。

    Returns:
        已编译恢复图、调用配置和暂停状态。
    """
    graph = build_error_recovery_graph(
        checkpointer=create_memory_checkpointer(),
    )
    config = {"configurable": {"thread_id": thread_id}}
    paused = graph.invoke(
        file_governance_to_recovery_state(state),
        config=config,
    )
    return graph, config, paused


def test_recovery_interrupt_exposes_all_supported_human_actions(
    tmp_path: Path,
) -> None:
    """耗尽自动恢复后应公开四种动作及其专用响应结构。"""
    state = create_human_recovery_state(tmp_path)

    _, _, paused = pause_human_recovery(
        state,
        thread_id="all-human-recovery-actions",
    )

    payload = paused["__interrupt__"][0].value
    assert payload["kind"] == "error_recovery"
    assert payload["allowed_actions"] == [
        "retry",
        "provide_path",
        "skip_file",
        "abort",
    ]
    assert payload["expected_schema"]["action"] == "<allowed_action>"
    assert paused["run"]["status"] == "waiting_human"


def test_recovery_interrupt_supports_manual_retry(tmp_path: Path) -> None:
    """人工 retry 应扩展一次显式重试并回到固定失败节点。"""
    state = create_human_recovery_state(tmp_path)
    graph, config, _ = pause_human_recovery(
        state,
        thread_id="manual-retry-action",
    )

    resumed = graph.invoke(
        Command(resume={"action": "retry", "note": "人工确认重试"}),
        config=config,
    )

    current_error = next(
        error
        for error in resumed["errors"]
        if error["id"] == resumed["recovery"]["current_error_id"]
    )
    assert resumed["recovery"]["action"] == "retry"
    assert resumed["recovery"]["resume_node"] == "run_inventory_subgraph"
    assert current_error["status"] == "retrying"
    assert current_error["retry_count"] == 2
    assert current_error["max_retries"] == 2


def test_recovery_interrupt_supports_skip_file(tmp_path: Path) -> None:
    """人工 skip_file 应登记文件级降级并保持错误非致命。"""
    state = create_human_recovery_state(tmp_path)
    graph, config, _ = pause_human_recovery(
        state,
        thread_id="manual-skip-file-action",
    )

    resumed = graph.invoke(
        Command(resume={"action": "skip_file"}),
        config=config,
    )

    assert resumed["recovery"]["action"] == "skip_file"
    assert resumed["degradations"][0]["action"] == "skip_file"
    assert resumed["degradations"][0]["affected_file_ids"] == ["file-001"]
    assert any(
        error["status"] == "fallback_applied" and error["fatal"] is False
        for error in resumed["errors"]
    )


def test_recovery_interrupt_supports_safe_replacement_path(
    tmp_path: Path,
) -> None:
    """人工 provide_path 应校验并替换只读输入目录，然后安排重试。"""
    state = create_human_recovery_state(tmp_path)
    replacement_root = tmp_path / "replacement-input"
    replacement_root.mkdir()
    graph, config, _ = pause_human_recovery(
        state,
        thread_id="manual-provide-path-action",
    )

    resumed = graph.invoke(
        Command(
            resume={
                "action": "provide_path",
                "replacement_path": str(replacement_root),
            }
        ),
        config=config,
    )

    assert resumed["recovery"]["action"] == "retry"
    assert resumed["request"]["root_directory"] == str(replacement_root.resolve())
    assert resumed["workspace"]["input_root"] == str(replacement_root.resolve())
    assert resumed["workspace"]["input_readonly"] is True


def test_recovery_interrupt_supports_abort(tmp_path: Path) -> None:
    """人工 abort 应终止恢复并把当前错误标记为最终失败。"""
    state = create_human_recovery_state(tmp_path)
    graph, config, _ = pause_human_recovery(
        state,
        thread_id="manual-abort-action",
    )

    resumed = graph.invoke(
        Command(resume={"action": "abort", "note": "用户终止"}),
        config=config,
    )

    assert resumed["recovery"]["action"] == "abort"
    assert resumed["run"]["status"] == "recovering"
    assert resumed["run"]["current_stage"] == "recovery_aborted"
    assert any(
        error["status"] == "failed" and error["fatal"] is True
        for error in resumed["errors"]
    )
