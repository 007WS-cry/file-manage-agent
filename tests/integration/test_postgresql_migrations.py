from __future__ import annotations

from alembic.config import Config
from sqlalchemy import inspect

from alembic import command
from app.storage.database import create_application_engine

"""本文件验证 Docker PostgreSQL 可以从空库迁移到 1.0.0，并完成回退与重放。"""


# 应用数据库完整迁移后必须存在的十张业务和运行时表。
EXPECTED_APPLICATION_TABLES = {
    "background_jobs",
    "context_summaries",
    "error_recovery_records",
    "governance_runs",
    "human_reviews",
    "memory_items",
    "node_execution_records",
    "scheduled_jobs",
    "tool_call_audits",
    "worker_leases",
}


def test_postgresql_migrations_upgrade_downgrade_and_replay(
    postgresql_database_url: str,
) -> None:
    """PostgreSQL 应支持迁移到 head、回退到 0004 并重新应用 0005。"""
    alembic_config = Config("alembic.ini")
    command.upgrade(alembic_config, "head")

    engine = create_application_engine(postgresql_database_url)
    try:
        inspector = inspect(engine)
        assert EXPECTED_APPLICATION_TABLES <= set(inspector.get_table_names())
        background_columns = {
            column["name"]
            for column in inspector.get_columns("background_jobs")
        }
        assert {"resume_count", "pending_interrupt", "resume_state"} <= (
            background_columns
        )
    finally:
        engine.dispose()

    command.downgrade(alembic_config, "0004_mcp_recovery_category")
    engine = create_application_engine(postgresql_database_url)
    try:
        downgraded_columns = {
            column["name"]
            for column in inspect(engine).get_columns("background_jobs")
        }
        assert "resume_count" not in downgraded_columns
    finally:
        engine.dispose()

    command.upgrade(alembic_config, "head")
