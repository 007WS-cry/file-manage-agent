from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from app.utils.runtime import paths_overlap

"""本模块配置 LangGraph 内存或 SQLite checkpoint，以支持人工暂停和恢复。"""


def create_memory_checkpointer() -> InMemorySaver:
    """创建仅在当前 Python 进程内有效的 LangGraph Checkpointer。

    Returns:
        适用于测试、单次脚本运行和嵌入式调用的 ``InMemorySaver``。
    """
    return InMemorySaver()


def validate_checkpoint_path(
    database_path: str | Path,
    *,
    input_root: str | Path | None = None,
) -> Path:
    """规范化 SQLite checkpoint 路径并保护只读输入目录。

    Args:
        database_path: SQLite checkpoint 数据库文件路径。
        input_root: 可选只读业务文件根目录。

    Returns:
        规范化后的数据库绝对路径。

    Raises:
        ValueError: 数据库路径是目录、符号链接或与输入目录重叠时抛出。
    """
    original_path = Path(database_path).expanduser()
    if original_path.is_symlink():
        raise ValueError("checkpoint 数据库不得是符号链接")
    resolved_path = original_path.resolve()
    if resolved_path.exists() and not resolved_path.is_file():
        raise ValueError("checkpoint 路径必须指向普通文件")
    if input_root is not None and paths_overlap(input_root, resolved_path):
        raise ValueError("checkpoint 数据库不得位于只读输入目录内或包含输入目录")
    return resolved_path


@contextmanager
def open_checkpointer(
    backend: Literal["memory", "sqlite"] = "sqlite",
    *,
    database_path: str | Path = ".artifacts/checkpoints/file-governance.sqlite3",
    input_root: str | Path | None = None,
) -> Iterator[BaseCheckpointSaver]:
    """按配置打开并在退出时关闭 LangGraph Checkpointer。

    ``memory`` 仅支持同一进程内恢复；``sqlite`` 会把每个图步骤按 ``thread_id``
    持久化到独立数据库，从而允许 CLI 在后续进程恢复人工审核。数据库只保存图
    状态和 checkpoint 元数据，不会写入或修改任何原始业务文件。

    Args:
        backend: ``memory`` 或 ``sqlite`` checkpoint 后端。
        database_path: SQLite 后端使用的数据库路径。
        input_root: 可选只读输入根目录，用于拒绝危险数据库位置。

    Yields:
        可传入 ``StateGraph.compile(checkpointer=...)`` 的 Checkpointer。

    Raises:
        ValueError: 后端名称或数据库路径不合法时抛出。
        OSError: SQLite 数据库父目录无法创建时抛出。
    """
    if backend == "memory":
        yield create_memory_checkpointer()
        return
    if backend != "sqlite":
        raise ValueError(f"不支持的 checkpoint 后端：{backend}")

    checkpoint_path = validate_checkpoint_path(
        database_path,
        input_root=input_root,
    )
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        checkpointer.setup()
        yield checkpointer
