from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from app.nodes.lifecycle import initialize_run
from app.services.reporting import build_report_state
from app.state.factories import (
    copy_recovery_state,
    create_initial_state,
    create_recovery_state,
)
from app.state.models import (
    ErrorRecord,
    FileGovernanceState,
    RecoveryGraphState,
    RecoveryState,
    RunState,
)
from app.utils.runtime import create_error_record

"""本模块验证 0.6.1 恢复状态类、初始默认值、旧状态补齐和错误构造兼容性。"""


def create_request(input_root: Path) -> dict[str, object]:
    """创建恢复状态单元测试使用的最小治理请求。

    Args:
        input_root: 测试使用的只读输入目录。

    Returns:
        可直接传给初始状态工厂的请求映射。
    """
    return {
        "root_directory": str(input_root),
        "recursive": True,
        "allowed_extensions": [".docx"],
        "max_files": 20,
        "grouping_similarity_threshold": 0.72,
        "auto_select_threshold": 0.82,
        "pdf_match_threshold": 0.82,
        "delivery_log_path": None,
        "use_llm_summary": False,
    }


def create_workspace(input_root: Path, temporary_root: Path) -> dict[str, object]:
    """创建与输入目录隔离的测试工作空间。

    Args:
        input_root: 测试使用的只读输入目录。
        temporary_root: pytest 提供的隔离临时根目录。

    Returns:
        可直接传给初始状态工厂的工作空间映射。
    """
    return {
        "input_root": str(input_root),
        "input_readonly": True,
        "artifact_root": str(temporary_root / "artifacts"),
        "report_root": str(temporary_root / "reports"),
    }


def test_initial_state_contains_empty_recovery_execution_and_degradation_state(
    tmp_path: Path,
) -> None:
    """新运行必须包含完整策略和三个无历史数据的恢复字段。"""
    input_root = tmp_path / "input"
    input_root.mkdir()

    state = create_initial_state(
        create_request(input_root),
        create_workspace(input_root, tmp_path),
    )

    assert state["recovery"]["policy"]["enabled"] is True
    assert state["recovery"]["pending_error_ids"] == []
    assert state["recovery"]["current_error_id"] is None
    assert state["recovery"]["action"] == "none"
    assert state["recovery"]["human"] == {
        "kind": "error_recovery",
        "pending_error_id": None,
        "allowed_actions": [],
        "selected_action": None,
        "replacement_path": None,
        "note": None,
    }
    assert state["node_executions"] == []
    assert state["degradations"] == []
    assert state["report"]["degradation_ids"] == []
    assert state["report"]["recovered_error_ids"] == []


def test_initial_state_accepts_recovery_policy_override(tmp_path: Path) -> None:
    """调用方可以在不接入恢复图的情况下覆盖确定性类别策略。"""
    input_root = tmp_path / "input"
    input_root.mkdir()

    state = create_initial_state(
        create_request(input_root),
        create_workspace(input_root, tmp_path),
        recovery_config={
            "categories": {
                "timeout": {
                    "max_retries": 3,
                }
            }
        },
    )

    timeout_policy = state["recovery"]["policy"]["category_policies"]["timeout"]
    assert timeout_policy["retryable"] is True
    assert timeout_policy["max_retries"] == 3


def test_initialize_run_backfills_v060_recovery_defaults(tmp_path: Path) -> None:
    """缺少恢复字段的 0.6.0 顶层状态必须在初始化节点中安全补齐。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    current_state = create_initial_state(
        create_request(input_root),
        create_workspace(input_root, tmp_path),
    )
    legacy_state = deepcopy(current_state)
    legacy_state.pop("recovery")
    legacy_state.pop("node_executions")
    legacy_state.pop("degradations")
    legacy_state["report"].pop("degradation_ids")
    legacy_state["report"].pop("recovered_error_ids")

    update = initialize_run(legacy_state)

    assert update["recovery"] == create_recovery_state()
    assert update["node_executions"] == []
    assert update["degradations"] == []
    assert update["report"]["degradation_ids"] == []
    assert update["report"]["recovered_error_ids"] == []
    assert update["run"]["status"] == "running"


def test_copy_recovery_state_breaks_mutable_references() -> None:
    """复制恢复状态后修改新对象不得污染原策略、错误队列或人工动作。"""
    original = create_recovery_state()
    original["pending_error_ids"].append("error-1")
    original["human"]["allowed_actions"].append("retry")

    copied = copy_recovery_state(original)
    copied["pending_error_ids"].append("error-2")
    copied["human"]["allowed_actions"].append("abort")
    copied["policy"]["category_policies"]["timeout"]["max_retries"] = 5

    assert original["pending_error_ids"] == ["error-1"]
    assert original["human"]["allowed_actions"] == ["retry"]
    assert original["policy"]["category_policies"]["timeout"]["max_retries"] == 2


def test_copy_recovery_state_rejects_unknown_dynamic_actions() -> None:
    """旧 checkpoint 中的未知动作不得成为动态节点或人工恢复命令。"""
    recovery = create_recovery_state()
    invalid = {
        **recovery,
        "action": "run_arbitrary_node",
    }

    with pytest.raises(ValueError, match="不是允许的恢复动作"):
        copy_recovery_state(invalid)


def test_report_state_indexes_degradations_and_recovered_errors(tmp_path: Path) -> None:
    """报告状态应保存降级记录和已恢复错误 ID，但不误收待处理错误。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    state = create_initial_state(
        create_request(input_root),
        create_workspace(input_root, tmp_path),
    )
    state["run"]["run_id"] = "report-recovery-index"
    state["degradations"] = [
        {
            "id": "degradation-1",
            "error_id": "error-recovered",
            "stage": "inventory",
            "action": "skip_file",
            "summary": "跳过损坏文件",
            "affected_file_ids": ["file-1"],
            "impact": "报告不包含该文件",
            "created_at": "2026-07-24T08:00:00+00:00",
        }
    ]
    state["errors"] = [
        create_error_record(
            stage="inventory",
            node_name="extract_docx_content",
            category="parse",
            message="损坏文件已跳过",
            status="fallback_applied",
            fallback="skip_file",
        ),
        create_error_record(
            stage="provider",
            node_name="invoke_model",
            category="timeout",
            message="等待重试",
            status="pending",
        ),
    ]

    report = build_report_state(state, "部分完成", "# 报告", [])

    assert report["degradation_ids"] == ["degradation-1"]
    assert report["recovered_error_ids"] == [state["errors"][0]["id"]]


def test_error_record_constructor_preserves_v060_identity_by_default() -> None:
    """旧调用方式必须保留稳定错误 ID、fatal 字段和已经处理的兼容终态。"""
    error = create_error_record(
        stage="inventory",
        node_name="extract_docx_content",
        category="parse",
        message="文件解析失败",
        related_file_id="file-1",
        fatal=False,
    )
    expected_id = hashlib.sha256(
        "\x1f".join(
            (
                "inventory",
                "extract_docx_content",
                "parse",
                "file-1",
                "文件解析失败",
            )
        ).encode()
    ).hexdigest()

    assert error["id"] == expected_id
    assert error["fatal"] is False
    assert error["status"] == "recovered"
    assert error["retryable"] is False
    assert error["retry_count"] == 0
    assert error["max_retries"] == 0
    assert error["fallback"] is None
    assert error["created_at"]


def test_error_record_constructor_accepts_recovery_metadata() -> None:
    """新调用方式必须保存 Task、节点执行、重试、降级和人工恢复元数据。"""
    error = create_error_record(
        stage="provider",
        node_name="invoke_model",
        category="timeout",
        message="调用超时",
        task_id="run-1:version_analysis",
        node_execution_id="execution-1",
        exception_type="TimeoutError",
        retryable=True,
        retry_count=1,
        max_retries=2,
        fallback="coordinator",
        requires_human=False,
        status="retrying",
        fatal=False,
    )

    assert error["task_id"] == "run-1:version_analysis"
    assert error["node_execution_id"] == "execution-1"
    assert error["exception_type"] == "TimeoutError"
    assert error["retryable"] is True
    assert error["retry_count"] == 1
    assert error["max_retries"] == 2
    assert error["fallback"] == "coordinator"
    assert error["status"] == "retrying"


def test_recovery_state_classes_are_declared_in_models() -> None:
    """顶层和未来恢复子图引用的所有状态类必须集中定义在 models.py。"""
    top_level_hints = get_type_hints(FileGovernanceState, include_extras=True)
    recovery_hints = get_type_hints(RecoveryState, include_extras=True)
    graph_hints = get_type_hints(RecoveryGraphState, include_extras=True)
    error_hints = get_type_hints(ErrorRecord, include_extras=True)
    run_status = get_type_hints(RunState)["status"]

    assert {"recovery", "node_executions", "degradations"} <= set(top_level_hints)
    assert "policy" in recovery_hints
    assert {"errors", "node_executions", "degradations", "recovery"} <= set(graph_hints)
    assert {
        "retryable",
        "retry_count",
        "max_retries",
        "fallback",
        "status",
        "created_at",
    } <= set(error_hints)
    assert "recovering" in get_args(run_status)
