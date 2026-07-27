from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.utils.runtime import paths_overlap

"""本模块创建并管理独立于 LangGraph Checkpointer 的 SQLAlchemy 应用数据库连接。"""


# 应用数据库默认保存在运行产物目录中，不与 LangGraph checkpoint 共用文件。
DEFAULT_APPLICATION_DATABASE_PATH = Path(
    ".artifacts/database/file-governance-app.sqlite3"
)

# SQLite 等待文件锁释放的默认秒数，避免短暂写竞争立即导致运行失败。
DEFAULT_SQLITE_TIMEOUT_SECONDS = 30.0

# 应用数据库 URL 的统一环境变量；PostgreSQL 凭据只允许通过该变量注入进程。
APPLICATION_DATABASE_URL_ENV = "FILE_GOVERNANCE_DATABASE_URL"

# SQLite 文件路径的兼容环境变量；未配置数据库 URL 时继续使用该变量。
APPLICATION_DATABASE_PATH_ENV = "FILE_GOVERNANCE_DATABASE_PATH"

# PostgreSQL 统一使用 psycopg 3 驱动，避免不同进程隐式选择不同 DBAPI。
POSTGRESQL_DRIVER_NAME = "postgresql+psycopg"

# 状态和运行时允许使用的应用数据库后端名称。
ApplicationDatabaseBackend = Literal["sqlite", "postgresql"]

# Engine 工厂接受本地 SQLite 路径或受信任进程配置提供的 SQLAlchemy URL。
ApplicationDatabaseTarget = str | Path


def is_application_database_url(database_target: ApplicationDatabaseTarget) -> bool:
    """判断应用数据库目标是否为显式 SQLAlchemy URL。

    Args:
        database_target: SQLite 文件路径或 SQLAlchemy 数据库 URL。

    Returns:
        字符串包含 URL scheme 分隔符时返回 True；Path 始终返回 False。
    """
    return isinstance(database_target, str) and "://" in database_target


def validate_application_database_path(
    database_path: str | Path,
    *,
    input_root: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
) -> Path:
    """规范化并校验应用数据库路径。

    应用数据库只允许写入调用方明确配置的普通文件路径。它不得位于只读业务输入
    目录内，也不得与 LangGraph checkpoint 数据库使用同一个文件。

    Args:
        database_path: 应用数据库 SQLite 文件路径。
        input_root: 可选只读业务文件根目录。
        checkpoint_path: 可选 LangGraph checkpoint SQLite 文件路径。

    Returns:
        经过展开和规范化的应用数据库绝对路径。

    Raises:
        ValueError: 路径为空、指向目录、使用符号链接、位于输入目录内或与
            checkpoint 数据库相同时抛出。
    """
    if not isinstance(database_path, (str, Path)):
        raise TypeError("database_path 必须是字符串或 Path")
    if isinstance(database_path, str) and not database_path.strip():
        raise ValueError("database_path 不得为空")

    original_path = Path(database_path).expanduser()
    if original_path.is_symlink():
        raise ValueError("应用数据库文件不得是符号链接")
    resolved_path = original_path.resolve()
    if resolved_path.exists() and not resolved_path.is_file():
        raise ValueError("应用数据库路径必须指向普通文件")
    if input_root is not None and paths_overlap(input_root, resolved_path):
        raise ValueError("应用数据库不得位于只读输入目录内或包含输入目录")
    if checkpoint_path is not None:
        resolved_checkpoint_path = Path(checkpoint_path).expanduser().resolve()
        if resolved_path == resolved_checkpoint_path:
            raise ValueError("应用数据库不得与 LangGraph checkpoint 共用同一个文件")
    return resolved_path


def build_application_database_url(database_path: str | Path) -> URL:
    """根据 SQLite 文件路径构造跨平台 SQLAlchemy URL。

    Args:
        database_path: 已配置的应用数据库文件路径。

    Returns:
        使用内置 pysqlite 驱动且不包含凭据的 SQLAlchemy URL。
    """
    resolved_path = validate_application_database_path(database_path)
    return URL.create(
        drivername="sqlite+pysqlite",
        database=str(resolved_path),
    )


def normalize_application_database_url(
    database_target: ApplicationDatabaseTarget,
    *,
    input_root: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
) -> URL:
    """把 SQLite 路径或受支持数据库 URL 规范化为 SQLAlchemy URL。

    PostgreSQL 仅接受 ``postgresql`` 或 ``postgresql+psycopg`` scheme，并统一
    使用 psycopg 3。SQLite URL 仍执行与文件路径相同的输入目录、checkpoint
    和符号链接隔离校验。

    Args:
        database_target: SQLite 文件路径或 PostgreSQL SQLAlchemy URL。
        input_root: 可选只读业务输入目录。
        checkpoint_path: 可选 LangGraph checkpoint SQLite 文件路径。

    Returns:
        可直接交给 SQLAlchemy Engine 的规范化 URL。

    Raises:
        TypeError: 数据库目标类型不合法时抛出。
        ValueError: URL scheme、PostgreSQL 连接字段或 SQLite 路径不安全时抛出。
    """
    if not isinstance(database_target, (str, Path)):
        raise TypeError("database_target 必须是字符串或 Path")
    if not is_application_database_url(database_target):
        resolved_path = validate_application_database_path(
            database_target,
            input_root=input_root,
            checkpoint_path=checkpoint_path,
        )
        return URL.create(
            drivername="sqlite+pysqlite",
            database=str(resolved_path),
        )

    raw_target = str(database_target).strip()
    if not raw_target:
        raise ValueError("database_target 不得为空")
    try:
        database_url = make_url(raw_target)
    except Exception as error:
        raise ValueError("应用数据库 URL 格式不合法") from error

    if database_url.drivername in {"postgresql", POSTGRESQL_DRIVER_NAME}:
        if not database_url.username:
            raise ValueError("PostgreSQL URL 必须包含用户名")
        if not database_url.host:
            raise ValueError("PostgreSQL URL 必须包含主机名")
        if not database_url.database:
            raise ValueError("PostgreSQL URL 必须包含数据库名")
        return database_url.set(drivername=POSTGRESQL_DRIVER_NAME)

    if database_url.drivername in {"sqlite", "sqlite+pysqlite"}:
        if database_url.database in {None, "", ":memory:"}:
            raise ValueError("应用数据库 SQLite URL 必须指向持久化文件")
        resolved_path = validate_application_database_path(
            database_url.database,
            input_root=input_root,
            checkpoint_path=checkpoint_path,
        )
        return URL.create(
            drivername="sqlite+pysqlite",
            database=str(resolved_path),
            query=database_url.query,
        )
    raise ValueError("应用数据库只支持 sqlite+pysqlite 或 postgresql+psycopg")


def get_application_database_backend(
    database_target: ApplicationDatabaseTarget,
) -> ApplicationDatabaseBackend:
    """返回应用数据库目标对应的稳定后端名称。

    Args:
        database_target: SQLite 文件路径或受支持 SQLAlchemy URL。

    Returns:
        ``sqlite`` 或 ``postgresql``。
    """
    database_url = normalize_application_database_url(database_target)
    return "postgresql" if database_url.drivername == POSTGRESQL_DRIVER_NAME else "sqlite"


def render_safe_application_database_target(
    database_target: ApplicationDatabaseTarget,
) -> str:
    """生成可用于诊断属性和日志的无密码数据库目标。

    Args:
        database_target: SQLite 文件路径或受支持 SQLAlchemy URL。

    Returns:
        SQLite 绝对路径，或隐藏密码后的 PostgreSQL URL。
    """
    database_url = normalize_application_database_url(database_target)
    if database_url.drivername.startswith("sqlite"):
        return str(database_url.database)
    return database_url.render_as_string(hide_password=True)


def resolve_application_database_target(
    *,
    database_url: str | None = None,
    database_path: str | Path | None = None,
) -> ApplicationDatabaseTarget:
    """按照 URL、路径、环境变量和默认值的优先级解析数据库目标。

    Args:
        database_url: 调用方显式传入的 PostgreSQL 或 SQLite SQLAlchemy URL。
        database_path: 调用方显式传入的 SQLite 文件路径。

    Returns:
        尚未建立连接、但已完成基本格式校验的数据库目标。

    Raises:
        ValueError: 同时显式提供 URL 和路径，或环境变量配置冲突时抛出。
    """
    if database_url is not None and not database_url.strip():
        raise ValueError("database_url 不得为空")
    if database_url is not None and database_path is not None:
        raise ValueError("database_url 与 database_path 不得同时显式提供")
    if database_url is not None:
        normalize_application_database_url(database_url)
        return database_url
    if database_path is not None:
        validate_application_database_path(database_path)
        return database_path

    configured_url = os.environ.get(APPLICATION_DATABASE_URL_ENV, "").strip()
    if configured_url:
        normalize_application_database_url(configured_url)
        return configured_url
    configured_path = os.environ.get(
        APPLICATION_DATABASE_PATH_ENV,
        str(DEFAULT_APPLICATION_DATABASE_PATH),
    )
    validate_application_database_path(configured_path)
    return configured_path


def build_application_database_state_reference(
    database_target: ApplicationDatabaseTarget,
) -> dict[str, object]:
    """构造可进入队列表和 checkpoint、但不包含数据库密码的连接引用。

    Args:
        database_target: 当前进程已经用于创建 Engine 的数据库目标。

    Returns:
        SQLite 返回绝对路径；PostgreSQL 只返回固定环境变量名称。

    Raises:
        RuntimeError: PostgreSQL 目标与当前固定环境变量不一致时抛出。
    """
    database_url = normalize_application_database_url(database_target)
    if database_url.drivername.startswith("sqlite"):
        return {
            "backend": "sqlite",
            "database_path": str(database_url.database),
            "database_url_env": None,
        }

    configured_url = os.environ.get(APPLICATION_DATABASE_URL_ENV, "").strip()
    if not configured_url:
        raise RuntimeError(
            f"PostgreSQL 必须通过 {APPLICATION_DATABASE_URL_ENV} 注入，"
            "禁止把含凭据 URL 写入任务状态"
        )
    configured = normalize_application_database_url(configured_url)
    if configured != database_url:
        raise RuntimeError(
            f"当前 PostgreSQL 目标必须与 {APPLICATION_DATABASE_URL_ENV} 完全一致"
        )
    return {
        "backend": "postgresql",
        "database_path": None,
        "database_url_env": APPLICATION_DATABASE_URL_ENV,
    }


def resolve_application_database_state_target(
    state: Mapping[str, object],
) -> ApplicationDatabaseTarget:
    """从应用数据库状态引用解析当前进程实际连接目标。

    本函数只允许读取固定的应用数据库 URL 环境变量，调用方不能通过状态字段
    指定其他环境变量名称，从而避免任意环境凭据读取。

    Args:
        state: ApplicationDatabaseState、MemoryState 或 ContextCompactState 映射。

    Returns:
        SQLite 文件路径或从固定环境变量读取的 PostgreSQL URL。

    Raises:
        ValueError: 后端、路径或环境变量引用不符合协议时抛出。
        RuntimeError: PostgreSQL URL 环境变量未配置时抛出。
    """
    backend = state.get("backend", "sqlite")
    if backend == "sqlite":
        database_path = state.get("database_path")
        if not isinstance(database_path, str) or not database_path.strip():
            raise ValueError("SQLite 应用数据库状态缺少 database_path")
        return database_path
    if backend != "postgresql":
        raise ValueError("应用数据库状态 backend 只能是 sqlite 或 postgresql")
    database_url_env = state.get("database_url_env")
    if database_url_env != APPLICATION_DATABASE_URL_ENV:
        raise ValueError(
            f"PostgreSQL 状态只能引用固定环境变量 {APPLICATION_DATABASE_URL_ENV}"
        )
    configured_url = os.environ.get(APPLICATION_DATABASE_URL_ENV, "").strip()
    if not configured_url:
        raise RuntimeError(f"环境变量 {APPLICATION_DATABASE_URL_ENV} 未配置")
    database_url = normalize_application_database_url(configured_url)
    if database_url.drivername != POSTGRESQL_DRIVER_NAME:
        raise ValueError("PostgreSQL 状态引用的环境变量不是 PostgreSQL URL")
    return configured_url


def _configure_sqlite_connection(
    dbapi_connection: object,
    connection_record: object,
) -> None:
    """为每个 SQLite DBAPI 连接启用外键并设置文件锁等待时间。

    本函数只执行固定 PRAGMA，不读取业务正文，也不接受来自用户或 LLM 的 SQL。

    Args:
        dbapi_connection: SQLAlchemy 连接池创建的 SQLite DBAPI 连接。
        connection_record: SQLAlchemy 连接池记录；当前实现不读取该对象。
    """
    del connection_record
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(
            f"PRAGMA busy_timeout={int(DEFAULT_SQLITE_TIMEOUT_SECONDS * 1000)}"
        )
    finally:
        cursor.close()


def create_application_engine(
    database_target: ApplicationDatabaseTarget = DEFAULT_APPLICATION_DATABASE_PATH,
    *,
    input_root: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
    echo: bool = False,
    timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS,
) -> Engine:
    """创建 SQLite 或 PostgreSQL 应用数据库 SQLAlchemy Engine。

    函数只会自动创建数据库文件的父目录，不会创建表。表结构由 Alembic 迁移
    管理；单元测试可以显式调用 ``Base.metadata.create_all()`` 创建临时结构。

    Args:
        database_target: 应用数据库 SQLite 文件路径或 PostgreSQL SQLAlchemy URL。
        input_root: 可选只读业务文件根目录。
        checkpoint_path: 可选 LangGraph checkpoint SQLite 文件路径。
        echo: 是否把 SQLAlchemy SQL 日志输出到标准日志。
        timeout_seconds: SQLite 等待文件锁释放的秒数。

    Returns:
        已配置连接健康检查和后端专用参数的 SQLAlchemy Engine。

    Raises:
        TypeError: ``echo`` 或 ``timeout_seconds`` 类型不合法时抛出。
        ValueError: URL、路径不安全或超时时间不大于零时抛出。
        OSError: 数据库父目录无法创建时抛出。
    """
    if not isinstance(echo, bool):
        raise TypeError("echo 必须是布尔值")
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds,
        (int, float),
    ):
        raise TypeError("timeout_seconds 必须是数字")
    normalized_timeout = float(timeout_seconds)
    if normalized_timeout <= 0:
        raise ValueError("timeout_seconds 必须大于零")

    database_url = normalize_application_database_url(
        database_target,
        input_root=input_root,
        checkpoint_path=checkpoint_path,
    )
    engine_options: dict[str, object] = {
        "echo": echo,
        "pool_pre_ping": True,
    }
    if database_url.drivername.startswith("sqlite"):
        resolved_path = Path(str(database_url.database))
        if resolved_path.parent.is_symlink():
            raise ValueError("应用数据库父目录不得是符号链接")
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        engine_options["connect_args"] = {"timeout": normalized_timeout}
    else:
        engine_options["pool_recycle"] = 1_800

    engine = create_engine(database_url, **engine_options)
    if database_url.drivername.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建绑定到指定 Engine 的同步 Session 工厂。

    Args:
        engine: 已由 ``create_application_engine()`` 创建的应用数据库 Engine。

    Returns:
        禁止提交后自动过期、且不自动 flush 的 SQLAlchemy Session 工厂。

    Raises:
        TypeError: ``engine`` 不是 SQLAlchemy Engine 时抛出。
    """
    if not isinstance(engine, Engine):
        raise TypeError("engine 必须是 SQLAlchemy Engine")
    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


@contextmanager
def open_application_session(
    session_factory: sessionmaker[Session],
) -> Iterator[Session]:
    """打开一次短生命周期应用数据库事务。

    上下文正常退出时提交，发生异常时回滚，并始终关闭 Session。Repository
    只负责查询、写入和 flush，不得在方法内部自行 commit。

    Args:
        session_factory: 绑定应用数据库 Engine 的 Session 工厂。

    Yields:
        当前事务独占使用的 SQLAlchemy Session。

    Raises:
        TypeError: ``session_factory`` 不可调用时抛出。
        Exception: 事务中的业务或数据库异常会在完成回滚后原样抛出。
    """
    if not callable(session_factory):
        raise TypeError("session_factory 必须可调用")
    session = session_factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
