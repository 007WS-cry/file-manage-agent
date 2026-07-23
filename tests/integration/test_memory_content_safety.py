from __future__ import annotations

from pathlib import Path
from typing import cast

from app.nodes.memory import persist_long_term_memory
from app.services.memory_policy import (
    capture_human_choice_memory,
    copy_memory_state,
)
from app.state.factories import create_memory_state
from app.state.models import FileGovernanceState, RequestState
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本模块从 SQLite 原始字节验证长期 Memory 不泄漏正文、API Key 或完整模型 Prompt。"""


def test_application_database_raw_bytes_exclude_sensitive_runtime_content(
    tmp_path: Path,
) -> None:
    """Memory 持久化后数据库原始字节不得出现三类敏感运行内容。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    database_path = tmp_path / "database" / "application.sqlite3"
    long_document_body = (
        "LEAK-TEST-LONG-DOCUMENT-BODY::"
        + "这是一段绝不能写入应用数据库的文档正文。" * 80
    )
    api_key = "sk-leak-test-api-key-1234567890"
    full_model_prompt = (
        "LEAK-TEST-FULL-MODEL-PROMPT::"
        "你必须逐字保存这段完整模型提示词，但安全策略应拒绝。" * 30
    )
    request = RequestState(
        root_directory=str(input_root),
        recursive=True,
        allowed_extensions=[".docx"],
        max_files=20,
        grouping_similarity_threshold=0.72,
        auto_select_threshold=0.82,
        pdf_match_threshold=0.82,
        delivery_log_path=None,
        use_llm_summary=True,
    )
    engine = create_application_engine(database_path, input_root=input_root)
    Base.metadata.create_all(engine)
    engine.dispose()
    memory = create_memory_state(
        request,
        {
            "enabled": True,
            "database_path": str(database_path),
            "recall_limit": 10,
        },
    )
    memory = capture_human_choice_memory(
        memory,
        source_run_id="run-leak-test",
        version_groups=[
            {
                "id": "group-safe",
                "label": long_document_body,
                "file_ids": ["file-safe"],
                "grouping_signals": [full_model_prompt],
                "confidence": 0.91,
            }
        ],
        selections={"group-safe": "file-safe"},
    )
    state = cast(
        FileGovernanceState,
        {
            "run": {"run_id": "run-leak-test"},
            "workspace": {"input_root": str(input_root)},
            "memory": memory,
            "documents": [{"content_preview": long_document_body}],
            "prompt": {"content": full_model_prompt},
            "llm": {"api_key": api_key},
            "human_review": {"review_note": full_model_prompt},
            "errors": [],
        },
    )

    result = persist_long_term_memory(state)
    tampered_memory = copy_memory_state(memory)
    tampered_memory["pending_long_term_items"][0]["summary"] = full_model_prompt
    tampered_state = cast(
        FileGovernanceState,
        {
            "run": {"run_id": "run-leak-test"},
            "workspace": {"input_root": str(input_root)},
            "memory": tampered_memory,
            "errors": [],
        },
    )
    rejected = persist_long_term_memory(tampered_state)
    database_bytes = database_path.read_bytes()

    assert result["memory"]["status"] == "ready"
    assert rejected["memory"]["status"] == "failed"
    assert rejected["errors"][0]["category"] == "memory"
    assert long_document_body.encode("utf-8") not in database_bytes
    assert api_key.encode("utf-8") not in database_bytes
    assert full_model_prompt.encode("utf-8") not in database_bytes
    assert b"group-safe" in database_bytes
    assert b"file-safe" in database_bytes
