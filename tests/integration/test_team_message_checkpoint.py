from __future__ import annotations

import json
from pathlib import Path

import pytest
from docx import Document

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state
from app.storage.checkpoints import open_checkpointer

"""本文件验证 SQLite checkpoint 只持久化 Team Protocol 摘要且不泄漏密钥或完整正文。"""

# checkpoint 泄漏测试使用的 API Key 环境变量名称。
CHECKPOINT_API_KEY_ENV = "FILE_MANAGE_AGENT_CHECKPOINT_TEST_API_KEY"

# 仅存在于进程环境、绝不能出现在状态或 SQLite 文件中的伪密钥值。
FORBIDDEN_API_KEY_VALUE = "sk-checkpoint-secret-must-never-be-persisted"

# 位于长正文尾部、超过内容预览边界且绝不能进入 checkpoint 的固定标记。
FULL_BODY_TAIL_MARKER = "FULL-MODEL-INPUT-TAIL-MUST-NOT-ENTER-CHECKPOINT"


def create_long_docx(path: Path, amount: int) -> None:
    """创建正文尾部带泄漏哨兵的长 DOCX 候选版本。

    Args:
        path: DOCX 文件输出路径。
        amount: 写入正文前部的合同金额。
    """
    document = Document()
    body = f"合同金额 CNY {amount}。" + ("共同条款内容。" * 700) + FULL_BODY_TAIL_MARKER
    document.add_paragraph(body)
    document.save(path)


def create_checkpoint_state(tmp_path: Path) -> dict:
    """创建预配置 OpenAI、关闭真实调用并启用三个摘要阶段的顶层状态。

    OpenAI Provider 配置只保存环境变量名称；``enabled=false`` 会强制统一 Client 使用
    Mock Provider，因此测试既能产生三个角色的消息和审计，也不会发起网络请求。

    Args:
        tmp_path: pytest 提供的隔离临时目录。

    Returns:
        可直接提交给带 SQLite Checkpointer 顶层图的完整状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    create_long_docx(input_root / "contract_v1.docx", 1_000)
    create_long_docx(input_root / "contract_v2.docx", 1_200)
    return create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.0,
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
            "provider": "openai",
            "model": "configured-but-disabled-model",
            "api_key_env": CHECKPOINT_API_KEY_ENV,
            "fallback_enabled": True,
        },
    )


def read_checkpoint_family(database_path: Path) -> bytes:
    """读取 SQLite 主文件及可能存在的事务附属文件字节。

    Args:
        database_path: SQLite checkpoint 主数据库路径。

    Returns:
        主数据库、WAL 和共享内存附属文件按名称拼接后的原始字节。
    """
    related_paths = sorted(database_path.parent.glob(f"{database_path.name}*"))
    return b"".join(path.read_bytes() for path in related_paths if path.is_file())


def test_checkpoint_excludes_api_key_and_full_model_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持久化并恢复 Team Message 后，密钥值、私有 Prompt 和长正文尾部均不得落盘。"""
    monkeypatch.setenv(CHECKPOINT_API_KEY_ENV, FORBIDDEN_API_KEY_VALUE)
    database_path = tmp_path / "checkpoints" / "team-message.sqlite3"
    graph_config = {"configurable": {"thread_id": "team-message-security-checkpoint"}}

    with open_checkpointer(
        "sqlite",
        database_path=database_path,
        input_root=tmp_path / "input",
    ) as checkpointer:
        graph = build_file_governance_graph(checkpointer=checkpointer)
        result = graph.invoke(create_checkpoint_state(tmp_path), config=graph_config)

    with open_checkpointer("sqlite", database_path=database_path) as checkpointer:
        restored_graph = build_file_governance_graph(checkpointer=checkpointer)
        restored_state = dict(restored_graph.get_state(graph_config).values)

    assert result["run"]["status"] == "completed"
    assert restored_state["team_messages"]
    assert restored_state["llm_calls"]
    assert {call["agent_id"] for call in restored_state["llm_calls"]} == {
        "content-subagent",
        "version-subagent",
        "evidence-subagent",
    }
    assert restored_state["llm"]["api_key_env"] == CHECKPOINT_API_KEY_ENV
    assert FORBIDDEN_API_KEY_VALUE not in restored_state["llm"].values()

    serialized_state = json.dumps(restored_state, ensure_ascii=False, default=str)
    assert FORBIDDEN_API_KEY_VALUE not in serialized_state
    assert FULL_BODY_TAIL_MARKER not in serialized_state
    assert "system_prompt" not in serialized_state
    assert "user_prompt" not in serialized_state

    checkpoint_bytes = read_checkpoint_family(database_path)
    assert FORBIDDEN_API_KEY_VALUE.encode("utf-8") not in checkpoint_bytes
    assert FULL_BODY_TAIL_MARKER.encode("utf-8") not in checkpoint_bytes
