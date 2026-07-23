from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from docx import Document
from langgraph.types import Command

from alembic import command
from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state
from app.storage.checkpoints import open_checkpointer
from app.storage.database import (
    build_application_database_url,
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.repositories import create_repository_bundle

"""本模块通过一次可恢复主图运行验证五张应用表的完整接线和数据库隔离。"""


# 仓库根目录用于定位正式 Alembic 配置与迁移脚本。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def create_docx(path: Path, text: str) -> None:
    """创建应用数据库端到端测试使用的真实 DOCX。

    Args:
        path: DOCX 输出路径。
        text: 写入文档的测试正文。
    """
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def migrate_application_database(database_path: Path) -> None:
    """使用正式 Alembic 脚本把隔离应用数据库升级到 head。

    Args:
        database_path: 当前测试独占的应用数据库文件路径。
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(PROJECT_ROOT / "alembic"),
    )
    config.set_main_option(
        "sqlalchemy.url",
        build_application_database_url(database_path).render_as_string(hide_password=False),
    )
    command.upgrade(config, "head")


def test_complete_run_wires_all_application_tables_and_keeps_input_readonly(
    tmp_path: Path,
) -> None:
    """一次暂停恢复运行应写满五表、隔离 checkpoint 且不修改原始文件。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    create_docx(
        input_root / "contract_v1.docx",
        "Release Contract Amount CNY 1000 Clause A " * 30,
    )
    create_docx(
        input_root / "contract_v2.docx",
        "Release Contract Amount CNY 1200 Clause A " * 30,
    )
    input_snapshots = {path.name: path.read_bytes() for path in input_root.iterdir()}
    application_path = tmp_path / "database" / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoints" / "langgraph.sqlite3"
    migrate_application_database(application_path)
    thread_id = "release-application-database"
    state = create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 1.0,
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
            "failure_policies": {
                "initialize_tool_audit_hook": "ignore",
                "flush_tool_audit_hook": "ignore",
                "cleanup_run_resources_hook": "ignore",
            },
        },
        memory_config={
            "enabled": True,
            "database_path": str(application_path),
            "recall_limit": 20,
        },
        context_compact_config={
            "enabled": True,
            "trigger_token_threshold": 1,
            "retained_preview_characters": 0,
            "persist_summaries": True,
            "database_path": str(application_path),
        },
        application_database_config={
            "enabled": True,
            "database_path": str(application_path),
        },
        checkpoint_path=checkpoint_path,
        thread_id=thread_id,
    )
    state["run"]["run_id"] = "run-release-database"
    config = {"configurable": {"thread_id": thread_id}}
    with open_checkpointer(
        "sqlite",
        database_path=checkpoint_path,
        input_root=input_root,
    ) as checkpointer:
        graph = build_file_governance_graph(checkpointer=checkpointer)
        paused = graph.invoke(state, config=config)
        group_id = paused["human_review"]["pending_group_ids"][0]
        selected_file_id = sorted(paused["version_groups"][0]["file_ids"])[-1]
        result = graph.invoke(
            Command(
                resume={
                    "selections": {group_id: selected_file_id},
                    "review_note": "sk-review-note-must-not-enter-app-db",
                }
            ),
            config=config,
        )

    assert result["run"]["status"] == "completed"
    assert result["run"]["thread_id"] == thread_id
    assert result["application_database"]["status"] == "ready"
    assert application_path.is_file()
    assert checkpoint_path.is_file()
    assert application_path.resolve() != checkpoint_path.resolve()

    verification_engine = create_application_engine(
        application_path,
        input_root=input_root,
        checkpoint_path=checkpoint_path,
    )
    session_factory = create_session_factory(verification_engine)
    with open_application_session(session_factory) as session:
        repositories = create_repository_bundle(session)
        run_record = repositories.governance_runs.get("run-release-database")
        assert run_record is not None
        assert run_record.thread_id == thread_id
        assert run_record.status == "completed"
        assert repositories.memory_items.list_by_namespace(result["memory"]["namespace"])
        assert repositories.context_summaries.list_by_run("run-release-database")
        audits = repositories.tool_call_audits.list_by_run("run-release-database")
        assert any(record.output_ref for record in audits)
        reviews = repositories.human_reviews.list_by_run("run-release-database")
        assert len(reviews) == 1
        assert reviews[0].selected_file_id == selected_file_id
        assert reviews[0].review_note is None
    verification_engine.dispose()

    application_bytes = application_path.read_bytes()
    assert b"Release Contract Amount" not in application_bytes
    assert b"sk-review-note-must-not-enter-app-db" not in application_bytes
    assert {path.name: path.read_bytes() for path in input_root.iterdir()} == input_snapshots
