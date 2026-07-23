from __future__ import annotations

from pathlib import Path

from app.hooks.builtin import flush_tool_audit_hook
from app.nodes.lifecycle import initialize_run
from app.state.factories import create_initial_state
from app.storage.artifacts import save_normalized_content_artifact
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.orm_models import Base
from app.storage.repositories import create_repository_bundle

"""本模块验证大型 Python Tool 输出只以受控产物引用进入应用数据库审计。"""


def test_large_tool_output_persists_only_bounded_summary_and_reference(
    tmp_path: Path,
) -> None:
    """标准化长正文应留在产物中，数据库只保存短摘要、引用和字节数。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    artifact_root = tmp_path / "artifacts"
    database_path = tmp_path / "database" / "application.sqlite3"
    leaked_marker = "sk-tool-output-must-not-enter-database"
    content_ref = save_normalized_content_artifact(
        artifact_root,
        "document-audit",
        {
            "normalized_text": leaked_marker * 2000,
            "structure": {"paragraph_count": 2000},
            "key_fields": {},
            "warnings": [],
        },
        input_root=input_root,
    )
    engine = create_application_engine(
        database_path,
        input_root=input_root,
    )
    Base.metadata.create_all(engine)
    engine.dispose()

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
            "artifact_root": str(artifact_root),
            "report_root": str(tmp_path / "reports"),
        },
        application_database_config={
            "enabled": True,
            "database_path": str(database_path),
        },
        thread_id="tool-audit-thread",
    )
    state["run"]["run_id"] = "run-tool-audit"
    state.update(initialize_run(state))
    state["files"] = [
        {
            "id": "file-audit",
            "absolute_path": str(input_root / "audit.docx"),
            "file_name": "audit.docx",
            "normalized_stem": "audit",
            "extension": ".docx",
            "size_bytes": 1024,
            "modified_at": "2026-07-23T00:00:00+00:00",
            "sha256": "a" * 64,
            "duplicate_of": None,
            "parse_status": "parsed",
            "parse_error": None,
        }
    ]
    state["documents"] = [
        {
            "id": "document-audit",
            "file_id": "file-audit",
            "parser_name": "docx-v1",
            "content_ref": content_ref,
            "content_preview": "",
            "normalized_digest": "b" * 64,
            "structure_summary": {"compacted": True},
            "key_fields": {},
            "warnings": [],
        }
    ]

    first_result = flush_tool_audit_hook(state)
    second_result = flush_tool_audit_hook(state)

    assert "2 条" in first_result["message"]
    assert "0 条" in second_result["message"]
    verification_engine = create_application_engine(
        database_path,
        input_root=input_root,
    )
    session_factory = create_session_factory(verification_engine)
    with open_application_session(session_factory) as session:
        records = create_repository_bundle(session).tool_call_audits.list_by_run("run-tool-audit")
        assert len(records) == 2
        parse_record = next(
            record for record in records if record.tool_name == "parse_docx_document"
        )
        assert parse_record.output_ref == content_ref
        assert parse_record.output_size_bytes == Path(content_ref).stat().st_size
        assert leaked_marker not in parse_record.output_summary
        assert parse_record.status == "success"
    verification_engine.dispose()

    assert leaked_marker.encode("utf-8") not in database_path.read_bytes()
    assert leaked_marker in Path(content_ref).read_text(encoding="utf-8")
