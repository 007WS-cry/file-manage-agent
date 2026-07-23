from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from app.utils.runtime import paths_overlap

"""本模块创建并管理独立于 LangGraph Checkpointer 的 SQLAlchemy 应用数据库连接。"""


# 应用数据库默认保存在运行产物目录中，不与 LangGraph checkpoint 共用文件。
DEFAULT_APPLICATION_DATABASE_PATH = Path(
    ".artifacts/database/file-governance-app.sqlite3"
)

# SQLite 等待文件锁释放的默认秒数，避免短暂写竞争立即导致运行失败。
DEFAULT_SQLITE_TIMEOUT_SECONDS = 30.0


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
    database_path: str | Path = DEFAULT_APPLICATION_DATABASE_PATH,
    *,
    input_root: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
    echo: bool = False,
    timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS,
) -> Engine:
    """创建应用数据库目录和 SQLAlchemy Engine。

    函数只会自动创建数据库文件的父目录，不会创建表。表结构由 Alembic 迁移
    管理；单元测试可以显式调用 ``Base.metadata.create_all()`` 创建临时结构。

    Args:
        database_path: 应用数据库 SQLite 文件路径。
        input_root: 可选只读业务文件根目录。
        checkpoint_path: 可选 LangGraph checkpoint SQLite 文件路径。
        echo: 是否把 SQLAlchemy SQL 日志输出到标准日志。
        timeout_seconds: SQLite 等待文件锁释放的秒数。

    Returns:
        已配置 SQLite 外键、锁等待和连接健康检查的 SQLAlchemy Engine。

    Raises:
        TypeError: ``echo`` 或 ``timeout_seconds`` 类型不合法时抛出。
        ValueError: 路径不安全或超时时间不大于零时抛出。
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

    resolved_path = validate_application_database_path(
        database_path,
        input_root=input_root,
        checkpoint_path=checkpoint_path,
    )
    if resolved_path.parent.is_symlink():
        raise ValueError("应用数据库父目录不得是符号链接")
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        URL.create(
            drivername="sqlite+pysqlite",
            database=str(resolved_path),
        ),
        echo=echo,
        pool_pre_ping=True,
        connect_args={"timeout": normalized_timeout},
    )
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
