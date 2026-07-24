from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from alembic import context
from app.storage.database import build_application_database_url
from app.storage.orm_models import Base

"""本模块配置七表应用数据库的 Alembic 在线和离线迁移，并自动准备 SQLite 父目录。"""


# Alembic 当前运行使用的全局配置对象。
config = context.config

# Alembic 自动比较表结构时使用的 SQLAlchemy MetaData。
target_metadata = Base.metadata

# 部署环境可通过该环境变量覆盖 alembic.ini 中的默认 SQLite 路径。
APPLICATION_DATABASE_PATH_ENV = "FILE_GOVERNANCE_DATABASE_PATH"

# 在线和离线迁移共用的结构比较及单迁移事务选项。
MIGRATION_CONTEXT_OPTIONS = {
    "compare_type": True,
    "transaction_per_migration": True,
}


def _apply_database_path_override() -> None:
    """把可选环境变量中的数据库文件路径转换为安全 SQLAlchemy URL。

    环境变量只表示本地 SQLite 文件路径，不接受任意数据库 URL，避免通过部署
    配置绕过当前版本的 SQLite 边界。
    """
    configured_path = os.environ.get(APPLICATION_DATABASE_PATH_ENV, "").strip()
    if not configured_path:
        return
    database_url = build_application_database_url(configured_path)
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string(hide_password=False),
    )


def _configure_logging() -> None:
    """根据 alembic.ini 初始化迁移日志。

    测试通过编程方式构造且没有配置文件名时跳过日志配置，避免影响 pytest
    已建立的日志处理器。
    """
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)


def _ensure_sqlite_parent(database_url: str) -> None:
    """为文件型 SQLite URL 自动创建数据库父目录。

    本函数只处理 Alembic 配置中的固定数据库 URL，不执行 SQL，也不会创建表。
    内存 SQLite 和非 SQLite URL 不进行目录操作。

    Args:
        database_url: Alembic 即将连接的 SQLAlchemy 数据库 URL。

    Raises:
        ValueError: SQLite URL 没有合法文件路径时抛出。
        OSError: 数据库父目录无法创建时抛出。
    """
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return
    if url.database in {None, "", ":memory:"}:
        if url.database == ":memory:":
            return
        raise ValueError("文件型 SQLite 应用数据库必须配置 database 路径")
    database_path = Path(url.database).expanduser().resolve()
    if database_path.exists() and not database_path.is_file():
        raise ValueError("Alembic 应用数据库路径必须指向普通文件")
    if database_path.is_symlink() or database_path.parent.is_symlink():
        raise ValueError("Alembic 应用数据库路径不得使用符号链接")
    database_path.parent.mkdir(parents=True, exist_ok=True)


def run_migrations_offline() -> None:
    """在不创建 Engine 的情况下生成离线迁移 SQL。

    离线模式只读取 URL 和迁移脚本，不连接数据库文件。
    """
    database_url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=database_url.startswith("sqlite"),
        **MIGRATION_CONTEXT_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """创建短生命周期 Engine 并执行在线迁移。

    数据库父目录会在建立 SQLite 连接前自动创建。迁移连接使用 ``NullPool``，
    完成升级或回退后立即释放。
    """
    configuration = config.get_section(config.config_ini_section) or {}
    database_url = configuration.get("sqlalchemy.url", "")
    _ensure_sqlite_parent(database_url)
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
            **MIGRATION_CONTEXT_OPTIONS,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


_apply_database_path_override()
_configure_logging()
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
