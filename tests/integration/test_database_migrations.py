from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from app.storage.database import build_application_database_url

"""本文件集成测试应用数据库 Alembic 迁移的升级、回退、重放和 checkpoint 隔离。"""


# 当前仓库根目录，用于定位 alembic.ini 和迁移脚本。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 首个应用数据库迁移应创建的五张业务表。
APPLICATION_TABLES = {
    "context_summaries",
    "governance_runs",
    "human_reviews",
    "memory_items",
    "tool_call_audits",
}


def create_alembic_config(database_path: Path) -> Config:
    """创建指向临时 SQLite 文件的 Alembic 测试配置。

    Args:
        database_path: 当前测试独占的应用数据库文件路径。

    Returns:
        使用仓库迁移目录和临时数据库 URL 的 Alembic Config。
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(PROJECT_ROOT / "alembic"),
    )
    config.set_main_option(
        "sqlalchemy.url",
        build_application_database_url(database_path).render_as_string(
            hide_password=False
        ),
    )
    return config


def read_table_names(database_path: Path) -> set[str]:
    """读取 SQLite 文件中的全部表名并立即释放检查 Engine。

    Args:
        database_path: 等待检查的 SQLite 数据库文件。

    Returns:
        数据库当前存在的表名集合。
    """
    engine = create_engine(build_application_database_url(database_path))
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_creates_parent_tables_and_matches_metadata(tmp_path: Path) -> None:
    """upgrade head 应自动创建父目录和五张表，且 ORM 元数据不存在新差异。"""
    database_path = tmp_path / "nested" / "application.sqlite3"
    config = create_alembic_config(database_path)

    command.upgrade(config, "head")

    assert database_path.is_file()
    assert APPLICATION_TABLES <= read_table_names(database_path)
    command.check(config)


def test_downgrade_and_reupgrade_are_reversible(tmp_path: Path) -> None:
    """首个迁移应能回退到 base，并能再次升级到相同表结构。"""
    database_path = tmp_path / "application.sqlite3"
    config = create_alembic_config(database_path)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    assert APPLICATION_TABLES.isdisjoint(read_table_names(database_path))

    command.upgrade(config, "head")

    assert APPLICATION_TABLES <= read_table_names(database_path)


def test_application_database_is_isolated_from_checkpoint_file(
    tmp_path: Path,
) -> None:
    """应用迁移不得创建或修改单独配置的 LangGraph checkpoint 文件。"""
    application_path = tmp_path / "database" / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoints" / "langgraph.sqlite3"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_sentinel = b"checkpoint-not-an-application-database"
    checkpoint_path.write_bytes(checkpoint_sentinel)
    config = create_alembic_config(application_path)

    command.upgrade(config, "head")

    assert application_path.is_file()
    assert checkpoint_path.read_bytes() == checkpoint_sentinel
    assert APPLICATION_TABLES <= read_table_names(application_path)


def test_environment_path_override_creates_configured_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境变量应安全覆盖 alembic.ini 路径并自动创建新的父目录。"""
    database_path = tmp_path / "environment" / "application.sqlite3"
    monkeypatch.setenv(
        "FILE_GOVERNANCE_DATABASE_PATH",
        str(database_path),
    )
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(PROJECT_ROOT / "alembic"),
    )

    command.upgrade(config, "head")

    assert database_path.is_file()
    assert APPLICATION_TABLES <= read_table_names(database_path)
