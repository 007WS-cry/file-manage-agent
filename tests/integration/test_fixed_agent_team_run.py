from __future__ import annotations

import json
from pathlib import Path

from docx import Document

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state

"""本文件端到端验证三个固定 Subagent 已接入真实业务阶段且状态保持最小化。"""

# 放在长正文尾部、不得进入内容预览、Team Message 或模型审计的测试标记。
FULL_BODY_TAIL_MARKER = "FULL-BODY-TAIL-MUST-NOT-ENTER-CHECKPOINT"


def create_versioned_docx(path: Path, amount: int) -> None:
    """创建具有相同主体和不同金额的长 DOCX 测试版本。

    Args:
        path: 测试文档输出路径。
        amount: 写入正文的合同金额。
    """
    document = Document()
    body = f"合同金额 CNY {amount}。" + ("共同条款内容。" * 600) + FULL_BODY_TAIL_MARKER
    document.add_paragraph(body)
    document.save(path)


def create_fixed_team_state(tmp_path: Path) -> dict:
    """创建启用 Version 摘要、但只使用离线 Mock Provider 的顶层状态。

    Args:
        tmp_path: 当前测试隔离目录。

    Returns:
        可直接提交给 File Governance 顶层图的完整初始状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    create_versioned_docx(input_root / "contract_v1.docx", 1_000)
    create_versioned_docx(input_root / "contract_v2.docx", 1_200)
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
            "use_llm_summary": True,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(tmp_path / "artifacts"),
            "report_root": str(tmp_path / "reports"),
        },
        llm_config={
            "enabled": False,
            "provider": "mock",
            "model": "mock-structured-v1",
        },
    )


def test_three_fixed_subagents_run_through_business_stages(tmp_path: Path) -> None:
    """Content、Version、Evidence 应经 Team Orchestration 返回最小消息和审计。"""
    result = build_file_governance_graph().invoke(
        create_fixed_team_state(tmp_path),
        config={"configurable": {"thread_id": "fixed-agent-team-run"}},
    )

    assert result["run"]["status"] == "completed"
    result_messages = [
        message
        for message in result["team_messages"]
        if message["message_type"] == "result"
    ]
    assert {message["sender"] for message in result_messages} == {
        "content-subagent",
        "version-subagent",
        "evidence-subagent",
    }
    expected_message_fields = {
        "message_id",
        "task_id",
        "sender",
        "receiver",
        "message_type",
        "status",
        "summary",
        "artifact_refs",
        "error",
        "created_at",
    }
    assert all(set(message) == expected_message_fields for message in result["team_messages"])
    assert {call["agent_id"] for call in result["llm_calls"]} == {
        "content-subagent",
        "version-subagent",
        "evidence-subagent",
    }
    assert all(member["status"] == "idle" for member in result["team"]["members"])
    assert all(member["current_task_id"] is None for member in result["team"]["members"])

    serialized = json.dumps(result, ensure_ascii=False, default=str)
    assert FULL_BODY_TAIL_MARKER not in serialized
    assert "system_prompt" not in serialized
    assert "user_prompt" not in serialized
