from __future__ import annotations

import json
from pathlib import Path

from langgraph.types import Command

from app.graphs.error_recovery import build_error_recovery_graph
from app.state.converters import file_governance_to_recovery_state
from app.state.factories import create_initial_state
from app.storage.checkpoints import create_memory_checkpointer
from app.utils.runtime import create_error_record

"""本文件验证恢复型人工 checkpoint 只保存状态引用，不携带正文、消息或报告内容。"""


# 不得进入 Error Recovery 状态、interrupt 载荷或 checkpoint 的敏感测试标记。
SENSITIVE_STATE_MARKER = "SENSITIVE-BUSINESS-CONTENT-MUST-NOT-ENTER-RECOVERY"


def create_reference_isolation_state(tmp_path: Path) -> dict:
    """创建包含敏感业务字段和一个待人工处理校验错误的顶层状态。

    Args:
        tmp_path: pytest 为当前测试提供的隔离目录。

    Returns:
        可转换为最小 RecoveryGraphState 的完整顶层状态。
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
        thread_id="state-reference-human-recovery",
    )
    state["run"].update(
        {
            "run_id": "state-reference-human-recovery-run",
            "thread_id": "state-reference-human-recovery",
            "status": "running",
            "current_stage": "request_validation",
            "started_at": "2026-07-24T08:00:00+00:00",
        }
    )
    state["documents"] = [
        {
            "id": "document-sensitive",
            "file_id": "file-sensitive",
            "parser_name": "docx-v1",
            "content_ref": "artifact://normalized/document-sensitive",
            "content_preview": SENSITIVE_STATE_MARKER,
            "normalized_digest": "a" * 64,
            "structure_summary": {},
            "key_fields": {},
            "warnings": [],
        }
    ]
    state["team_messages"] = [
        {
            "message_id": "message-sensitive",
            "task_id": "task-sensitive",
            "sender": "content-subagent",
            "receiver": "coordinator-agent",
            "message_type": "result",
            "status": "delivered",
            "summary": SENSITIVE_STATE_MARKER,
            "artifact_refs": [],
            "error": None,
            "created_at": "2026-07-24T08:00:00+00:00",
        }
    ]
    state["report"]["report_markdown"] = SENSITIVE_STATE_MARKER
    state["errors"] = [
        create_error_record(
            stage="request_validation",
            node_name="validate_request",
            category="validation",
            message="输入目录需要人工确认",
            requires_human=True,
            status="pending",
            fatal=True,
        )
    ]
    return state


def test_human_recovery_checkpoint_contains_references_only(
    tmp_path: Path,
) -> None:
    """人工恢复暂停点应只公开错误 ID 和动作协议，并能从同一 checkpoint 终止。"""
    state = create_reference_isolation_state(tmp_path)
    recovery_input = file_governance_to_recovery_state(state)
    graph = build_error_recovery_graph(
        checkpointer=create_memory_checkpointer(),
    )
    config = {
        "configurable": {
            "thread_id": "state-reference-human-recovery",
        }
    }

    paused = graph.invoke(recovery_input, config=config)
    payload = paused["__interrupt__"][0].value
    checkpoint_values = graph.get_state(config).values
    serialized_input = json.dumps(recovery_input, ensure_ascii=False, default=str)
    serialized_paused = json.dumps(paused, ensure_ascii=False, default=str)
    serialized_checkpoint = json.dumps(
        checkpoint_values,
        ensure_ascii=False,
        default=str,
    )

    assert set(payload) == {
        "kind",
        "instruction",
        "error_id",
        "allowed_actions",
        "expected_schema",
    }
    assert payload["kind"] == "error_recovery"
    assert payload["error_id"] == state["errors"][0]["id"]
    assert set(recovery_input) == {
        "run",
        "request",
        "workspace",
        "application_database",
        "tasks",
        "errors",
        "node_executions",
        "degradations",
        "recovery",
    }
    assert SENSITIVE_STATE_MARKER not in serialized_input
    assert SENSITIVE_STATE_MARKER not in serialized_paused
    assert SENSITIVE_STATE_MARKER not in serialized_checkpoint
    assert "documents" not in checkpoint_values
    assert "team_messages" not in checkpoint_values
    assert "report" not in checkpoint_values

    resumed = graph.invoke(
        Command(resume={"action": "abort", "note": "引用隔离测试结束"}),
        config=config,
    )
    assert resumed["recovery"]["action"] == "abort"
    assert any(error["status"] == "failed" for error in resumed["errors"])
