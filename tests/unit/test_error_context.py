from __future__ import annotations

from app.graphs.routers import route_version_analysis_result
from app.services.recovery_policy import create_recovery_policy_state
from app.state.models import ErrorContextState
from app.utils.error_context import create_node_error, is_error_unresolved
from app.utils.runtime import create_error_record

"""本文件验证 0.6.4 统一错误上下文、恢复进度继承和历史终态过滤契约。"""


def create_memory_error_state() -> dict:
    """创建仅包含统一错误上下文的 Memory 节点测试状态。

    Returns:
        启用 Memory 有限重试与 no_memory 降级的最小状态。
    """
    policy = create_recovery_policy_state(
        {
            "enabled": True,
            "categories": {
                "memory": {
                    "retryable": True,
                    "max_retries": 1,
                    "fallback": "no_memory",
                    "requires_human": False,
                }
            },
        }
    )
    return {
        "error_context": ErrorContextState(
            run_id="run-error-context",
            task_id="run-error-context:inventory",
            task_execution_id="run-error-context:inventory:execution",
            policy=policy,
        ),
        "errors": [],
    }


def test_node_error_contains_complete_recovery_identity() -> None:
    """节点错误必须同时携带 Task、节点执行、重试和降级字段。"""
    error = create_node_error(
        create_memory_error_state(),
        stage="memory_recall",
        node_name="recall_long_term_memory",
        category="memory",
        message="长期 Memory 暂时不可用。",
    )

    assert error["task_id"] == "run-error-context:inventory"
    assert error["node_execution_id"].startswith("node-error-")
    assert error["retryable"] is True
    assert error["retry_count"] == 0
    assert error["max_retries"] == 1
    assert error["fallback"] == "no_memory"
    assert error["status"] == "pending"
    assert error["fatal"] is True


def test_replayed_node_error_preserves_retry_progress() -> None:
    """同一节点执行重放失败时不得把已经登记的重试次数归零。"""
    state = create_memory_error_state()
    first_error = create_node_error(
        state,
        stage="memory_recall",
        node_name="recall_long_term_memory",
        category="memory",
        message="长期 Memory 暂时不可用。",
    )
    state["errors"] = [
        {
            **first_error,
            "retry_count": 1,
            "status": "retrying",
            "fatal": False,
        }
    ]

    replayed_error = create_node_error(
        state,
        stage="memory_recall",
        node_name="recall_long_term_memory",
        category="memory",
        message="长期 Memory 暂时不可用。",
    )

    assert replayed_error["id"] == first_error["id"]
    assert replayed_error["retry_count"] == 1
    assert replayed_error["status"] == "pending"
    assert replayed_error["fatal"] is True


def test_recovered_historical_error_does_not_retrigger_router() -> None:
    """历史已恢复错误即使保留旧 fatal 值，也不得再次触发顶层 Recovery。"""
    recovered_error = create_error_record(
        stage="version_analysis",
        node_name="build_version_edges",
        category="validation",
        message="历史错误已恢复。",
        status="recovered",
        fatal=True,
    )
    state = {"errors": [recovered_error]}

    assert is_error_unresolved(recovered_error) is False
    assert route_version_analysis_result(state) == "success"
