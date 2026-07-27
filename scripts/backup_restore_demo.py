from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

"""本脚本以 SQLite 新副本或 Docker PostgreSQL 临时库演示应用数据库备份、恢复和校验。"""


# 当前仓库根目录，用于定位受版本控制的 Compose 文件。
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# PostgreSQL 演示允许操作的固定 Compose 服务名。
POSTGRESQL_SERVICE_NAME = "postgres"

# PostgreSQL 恢复演示创建的数据库必须使用此前缀，防止误指向业务库。
RESTORE_DATABASE_PREFIX = "file_governance_restore_"

# Docker 子进程的最长等待秒数。
DOCKER_COMMAND_TIMEOUT_SECONDS = 120


def build_argument_parser() -> argparse.ArgumentParser:
    """构建数据库备份恢复演示的命令行解析器。

    Returns:
        包含后端、动作、工作目录和受控恢复参数的解析器。
    """
    parser = argparse.ArgumentParser(
        description="演示 1.0.0 SQLite 或 Docker PostgreSQL 数据库的备份与恢复校验。"
    )
    parser.add_argument(
        "--backend",
        choices=("sqlite", "postgresql"),
        default="sqlite",
        help="需要演示的应用数据库后端。",
    )
    parser.add_argument(
        "--action",
        choices=("backup", "restore", "roundtrip"),
        default="roundtrip",
        help="仅备份、恢复到新目标，或执行备份与临时恢复闭环。",
    )
    parser.add_argument(
        "--work-directory",
        type=Path,
        default=Path(".artifacts/backup-restore"),
        help="保存备份、恢复副本和结果摘要的受控目录。",
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        help="SQLite 源数据库路径；SQLite 后端必需。",
    )
    parser.add_argument(
        "--backup-path",
        type=Path,
        help="备份动作的输出路径，或恢复动作的已有备份路径。",
    )
    parser.add_argument(
        "--restore-target",
        type=Path,
        help="SQLite restore 动作使用的全新数据库路径。",
    )
    parser.add_argument(
        "--restore-database-name",
        help=f"PostgreSQL restore 动作创建的新库名，必须以 {RESTORE_DATABASE_PREFIX} 开头。",
    )
    parser.add_argument(
        "--confirm-restore",
        action="store_true",
        help="确认 restore 动作会创建一个新的 SQLite 文件或 Docker PostgreSQL 数据库。",
    )
    parser.add_argument(
        "--compose-project-name",
        default="file-manage-agent",
        help="已经启动 PostgreSQL 拓扑的 Docker Compose 项目名。",
    )
    return parser


def resolve_work_directory(work_directory: Path) -> Path:
    """规范化并创建不使用符号链接的演示工作目录。

    Args:
        work_directory: 用户显式提供的工作目录。

    Returns:
        已创建的绝对目录。

    Raises:
        ValueError: 路径是符号链接或已存在普通文件时抛出。
    """
    candidate = work_directory.expanduser()
    if candidate.is_symlink():
        raise ValueError("备份恢复工作目录不得是符号链接")
    resolved = candidate.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("备份恢复工作路径必须是目录")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def validate_sqlite_source(database_path: Path | None) -> Path:
    """校验 SQLite 源数据库是现有普通文件。

    Args:
        database_path: 命令行提供的可选源数据库路径。

    Returns:
        已解析的 SQLite 普通文件路径。

    Raises:
        ValueError: 参数缺失、使用符号链接或不是普通文件时抛出。
    """
    if database_path is None:
        raise ValueError("SQLite 后端必须提供 --database-path")
    candidate = database_path.expanduser()
    if candidate.is_symlink():
        raise ValueError("SQLite 源数据库不得是符号链接")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("SQLite 源数据库必须是普通文件")
    return resolved


def validate_new_output_path(path: Path, *, work_directory: Path) -> Path:
    """校验待创建文件位于工作目录内且不会覆盖现有对象。

    Args:
        path: 用户提供或脚本生成的输出路径。
        work_directory: 已验证的受控工作目录。

    Returns:
        可以安全新建的绝对文件路径。

    Raises:
        ValueError: 路径越界、使用符号链接或已经存在时抛出。
    """
    candidate = path.expanduser()
    if candidate.is_symlink() or candidate.parent.is_symlink():
        raise ValueError("备份或恢复输出路径不得使用符号链接")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(work_directory)
    except ValueError as error:
        raise ValueError("备份或恢复输出必须位于 --work-directory 内") from error
    if resolved.exists():
        raise ValueError(f"输出路径已存在，拒绝覆盖：{resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def sqlite_backup(source_path: Path, backup_path: Path) -> None:
    """使用 SQLite 在线备份 API 创建一致性副本。

    Args:
        source_path: 已验证的源 SQLite 数据库。
        backup_path: 不存在的备份输出路径。
    """
    source_uri = f"{source_path.as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source:
        with sqlite3.connect(backup_path) as destination:
            source.backup(destination)


def sqlite_restore(backup_path: Path, restore_path: Path) -> None:
    """把 SQLite 备份恢复到一个全新的数据库文件。

    Args:
        backup_path: 已有且通过完整性校验的备份文件。
        restore_path: 不存在的恢复目标路径。
    """
    source_uri = f"{backup_path.as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source:
        with sqlite3.connect(restore_path) as destination:
            source.backup(destination)


def sqlite_table_counts(database_path: Path) -> dict[str, int]:
    """读取 SQLite 用户表行数并执行完整性检查。

    Args:
        database_path: 等待验证的 SQLite 数据库。

    Returns:
        按表名排序的行数映射。

    Raises:
        RuntimeError: SQLite 完整性检查失败时抛出。
    """
    database_uri = f"{database_path.as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError(f"SQLite 完整性检查失败：{integrity}")
        table_names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        counts: dict[str, int] = {}
        for table_name in table_names:
            escaped_name = table_name.replace('"', '""')
            row = connection.execute(
                f'SELECT COUNT(*) FROM "{escaped_name}"'
            ).fetchone()
            counts[table_name] = int(row[0])
        return counts


def run_sqlite_demo(
    *,
    action: Literal["backup", "restore", "roundtrip"],
    work_directory: Path,
    database_path: Path | None,
    backup_path: Path | None,
    restore_target: Path | None,
    confirm_restore: bool,
) -> dict[str, Any]:
    """执行 SQLite 备份、全新目标恢复或非破坏性闭环。

    Args:
        action: ``backup``、``restore`` 或 ``roundtrip``。
        work_directory: 所有新文件必须位于其中的受控目录。
        database_path: 源 SQLite 数据库路径。
        backup_path: 可选备份输出或恢复输入路径。
        restore_target: restore 动作的全新目标路径。
        confirm_restore: 是否明确确认 restore 动作。

    Returns:
        操作路径、表行数和校验状态摘要。

    Raises:
        ValueError: 恢复未确认、路径缺失、越界或可能覆盖时抛出。
        RuntimeError: 恢复后的表行数与源备份不一致时抛出。
    """
    if action == "restore":
        if not confirm_restore:
            raise ValueError("restore 动作必须显式提供 --confirm-restore")
        if backup_path is None or restore_target is None:
            raise ValueError("SQLite restore 必须提供 --backup-path 和 --restore-target")
        source_backup = validate_sqlite_source(backup_path)
        target = validate_new_output_path(
            restore_target,
            work_directory=work_directory,
        )
        expected_counts = sqlite_table_counts(source_backup)
        sqlite_restore(source_backup, target)
        actual_counts = sqlite_table_counts(target)
        if actual_counts != expected_counts:
            raise RuntimeError("SQLite 恢复后的表行数与备份不一致")
        return {
            "action": action,
            "backup_path": str(source_backup),
            "restore_path": str(target),
            "verified": True,
            "table_counts": actual_counts,
        }

    source = validate_sqlite_source(database_path)
    target_backup = validate_new_output_path(
        backup_path or work_directory / "file-governance.sqlite3.bak",
        work_directory=work_directory,
    )
    source_counts = sqlite_table_counts(source)
    sqlite_backup(source, target_backup)
    backup_counts = sqlite_table_counts(target_backup)
    if backup_counts != source_counts:
        raise RuntimeError("SQLite 备份后的表行数与源数据库不一致")
    result: dict[str, Any] = {
        "action": action,
        "database_path": str(source),
        "backup_path": str(target_backup),
        "verified": True,
        "table_counts": backup_counts,
    }
    if action == "roundtrip":
        restore_path = validate_new_output_path(
            work_directory / "file-governance-restored.sqlite3",
            work_directory=work_directory,
        )
        sqlite_restore(target_backup, restore_path)
        restored_counts = sqlite_table_counts(restore_path)
        if restored_counts != source_counts:
            raise RuntimeError("SQLite 临时恢复后的表行数与源数据库不一致")
        result["restore_path"] = str(restore_path)
        result["restored_table_counts"] = restored_counts
    return result


def build_compose_command(
    compose_project_name: str,
    *arguments: str,
) -> list[str]:
    """构造仅操作仓库 PostgreSQL Compose 服务的命令。

    Args:
        compose_project_name: 已启动拓扑的 Compose 项目名。
        arguments: 传给 ``docker compose`` 的后续固定参数。

    Returns:
        可直接交给 subprocess 的参数列表。

    Raises:
        ValueError: 项目名为空或包含不受支持字符时抛出。
    """
    normalized_name = compose_project_name.strip()
    if not normalized_name or not all(
        character.isalnum() or character in {"-", "_"}
        for character in normalized_name
    ):
        raise ValueError("Compose 项目名只能包含字母、数字、连字符和下划线")
    return [
        "docker",
        "compose",
        "--project-name",
        normalized_name,
        "--project-directory",
        str(PROJECT_ROOT),
        "-f",
        str(PROJECT_ROOT / "docker-compose.yml"),
        "-f",
        str(PROJECT_ROOT / "docker-compose.postgresql.yml"),
        *arguments,
    ]


def run_compose_process(
    compose_project_name: str,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """执行一个受限 Docker Compose 子进程并返回二进制输出。

    Args:
        compose_project_name: 已启动 PostgreSQL 演示拓扑的项目名。
        arguments: 固定服务和数据库工具参数。
        input_bytes: 可选传给 pg_restore 的备份字节。

    Returns:
        返回码为零的已完成子进程。

    Raises:
        RuntimeError: Docker 不可用、命令超时或返回非零时抛出。
    """
    if shutil.which("docker") is None:
        raise RuntimeError("当前环境没有 docker 命令")
    command_line = build_compose_command(compose_project_name, *arguments)
    try:
        result = subprocess.run(
            command_line,
            cwd=PROJECT_ROOT,
            env=os.environ.copy(),
            input=input_bytes,
            capture_output=True,
            timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Docker PostgreSQL 备份恢复命令超时") from error
    if result.returncode != 0:
        error_text = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Docker PostgreSQL 命令失败：{error_text}")
    return result


def validate_restore_database_name(database_name: str | None) -> str:
    """校验 PostgreSQL 恢复目标只能是专用新库名。

    Args:
        database_name: 用户显式提供或脚本生成的数据库名。

    Returns:
        可传给 PostgreSQL 工具的受控数据库名。

    Raises:
        ValueError: 名称前缀或字符不符合恢复隔离约束时抛出。
    """
    normalized_name = (database_name or "").strip()
    if not normalized_name.startswith(RESTORE_DATABASE_PREFIX):
        raise ValueError(
            f"PostgreSQL 恢复数据库名必须以 {RESTORE_DATABASE_PREFIX} 开头"
        )
    if not all(character.isalnum() or character == "_" for character in normalized_name):
        raise ValueError("PostgreSQL 恢复数据库名只能包含字母、数字和下划线")
    return normalized_name


def postgresql_service_environment() -> tuple[str, str]:
    """读取 Compose PostgreSQL 服务使用的非敏感用户名和数据库名。

    Returns:
        ``(用户名, 数据库名)``。
    """
    return (
        os.environ.get("FILE_GOVERNANCE_POSTGRES_USER", "file_governance"),
        os.environ.get("FILE_GOVERNANCE_POSTGRES_DB", "file_governance"),
    )


def create_postgresql_backup(
    *,
    compose_project_name: str,
    backup_path: Path,
) -> None:
    """通过容器内 pg_dump 创建 PostgreSQL 自定义格式备份。

    Args:
        compose_project_name: 已启动拓扑的 Compose 项目名。
        backup_path: 不存在的本地备份输出路径。
    """
    database_user, database_name = postgresql_service_environment()
    result = run_compose_process(
        compose_project_name,
        "exec",
        "-T",
        POSTGRESQL_SERVICE_NAME,
        "pg_dump",
        "--username",
        database_user,
        "--dbname",
        database_name,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
    )
    if not result.stdout:
        raise RuntimeError("Docker PostgreSQL pg_dump 没有产生备份内容")
    backup_path.write_bytes(result.stdout)


def create_postgresql_restore_database(
    *,
    compose_project_name: str,
    database_name: str,
) -> None:
    """在 Docker PostgreSQL 中创建一个专用恢复数据库。

    Args:
        compose_project_name: 已启动拓扑的 Compose 项目名。
        database_name: 已验证且此前不存在的恢复数据库名。
    """
    database_user, _ = postgresql_service_environment()
    run_compose_process(
        compose_project_name,
        "exec",
        "-T",
        POSTGRESQL_SERVICE_NAME,
        "createdb",
        "--username",
        database_user,
        database_name,
    )


def restore_postgresql_backup(
    *,
    compose_project_name: str,
    database_name: str,
    backup_bytes: bytes,
) -> None:
    """把自定义格式备份恢复到已经创建的专用 Docker 数据库。

    Args:
        compose_project_name: 已启动拓扑的 Compose 项目名。
        database_name: 已创建的专用恢复数据库名。
        backup_bytes: pg_dump 自定义格式备份内容。
    """
    database_user, _ = postgresql_service_environment()
    run_compose_process(
        compose_project_name,
        "exec",
        "-T",
        POSTGRESQL_SERVICE_NAME,
        "pg_restore",
        "--username",
        database_user,
        "--dbname",
        database_name,
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        input_bytes=backup_bytes,
    )


def verify_postgresql_restore(
    *,
    compose_project_name: str,
    database_name: str,
) -> str:
    """查询恢复数据库的 Alembic 版本以验证结构可用。

    Args:
        compose_project_name: 已启动拓扑的 Compose 项目名。
        database_name: 已恢复的专用数据库名。

    Returns:
        ``alembic_version`` 表中的版本号。

    Raises:
        RuntimeError: 版本表为空时抛出。
    """
    database_user, _ = postgresql_service_environment()
    result = run_compose_process(
        compose_project_name,
        "exec",
        "-T",
        POSTGRESQL_SERVICE_NAME,
        "psql",
        "--username",
        database_user,
        "--dbname",
        database_name,
        "--tuples-only",
        "--no-align",
        "--command",
        "SELECT version_num FROM alembic_version;",
    )
    version = result.stdout.decode("utf-8", errors="replace").strip()
    if not version:
        raise RuntimeError("PostgreSQL 恢复数据库缺少 Alembic 版本")
    return version


def drop_postgresql_restore_database(
    *,
    compose_project_name: str,
    database_name: str,
) -> None:
    """删除 roundtrip 动作创建的专用临时 PostgreSQL 数据库。

    Args:
        compose_project_name: 已启动拓扑的 Compose 项目名。
        database_name: 带固定恢复前缀的临时数据库名。
    """
    validated_name = validate_restore_database_name(database_name)
    database_user, _ = postgresql_service_environment()
    run_compose_process(
        compose_project_name,
        "exec",
        "-T",
        POSTGRESQL_SERVICE_NAME,
        "dropdb",
        "--username",
        database_user,
        "--if-exists",
        validated_name,
    )


def run_postgresql_demo(
    *,
    action: Literal["backup", "restore", "roundtrip"],
    work_directory: Path,
    backup_path: Path | None,
    restore_database_name: str | None,
    confirm_restore: bool,
    compose_project_name: str,
) -> dict[str, Any]:
    """执行 Docker PostgreSQL 备份或专用数据库恢复演示。

    Args:
        action: ``backup``、``restore`` 或 ``roundtrip``。
        work_directory: 本地备份文件所在的受控目录。
        backup_path: 可选备份输出或恢复输入文件。
        restore_database_name: restore 动作显式创建的新数据库名。
        confirm_restore: 是否确认创建恢复数据库。
        compose_project_name: 已启动拓扑的 Compose 项目名。

    Returns:
        备份路径、临时数据库和 Alembic 版本校验摘要。

    Raises:
        ValueError: 恢复未确认、备份缺失或数据库名不安全时抛出。
    """
    if action == "restore":
        if not confirm_restore:
            raise ValueError("PostgreSQL restore 必须显式提供 --confirm-restore")
        if backup_path is None:
            raise ValueError("PostgreSQL restore 必须提供 --backup-path")
        source_backup = backup_path.expanduser()
        if source_backup.is_symlink():
            raise ValueError("PostgreSQL 备份输入不得是符号链接")
        source_backup = source_backup.resolve(strict=True)
        if not source_backup.is_file():
            raise ValueError("PostgreSQL 备份输入必须是普通文件")
        database_name = validate_restore_database_name(restore_database_name)
        create_postgresql_restore_database(
            compose_project_name=compose_project_name,
            database_name=database_name,
        )
        restore_postgresql_backup(
            compose_project_name=compose_project_name,
            database_name=database_name,
            backup_bytes=source_backup.read_bytes(),
        )
        version = verify_postgresql_restore(
            compose_project_name=compose_project_name,
            database_name=database_name,
        )
        return {
            "action": action,
            "backup_path": str(source_backup),
            "restore_database": database_name,
            "alembic_version": version,
            "verified": True,
        }

    target_backup = validate_new_output_path(
        backup_path or work_directory / "file-governance-postgresql.dump",
        work_directory=work_directory,
    )
    create_postgresql_backup(
        compose_project_name=compose_project_name,
        backup_path=target_backup,
    )
    result: dict[str, Any] = {
        "action": action,
        "backup_path": str(target_backup),
        "backup_size_bytes": target_backup.stat().st_size,
        "verified": True,
    }
    if action == "roundtrip":
        temporary_name = validate_restore_database_name(
            f"{RESTORE_DATABASE_PREFIX}{uuid4().hex[:12]}"
        )
        create_postgresql_restore_database(
            compose_project_name=compose_project_name,
            database_name=temporary_name,
        )
        try:
            restore_postgresql_backup(
                compose_project_name=compose_project_name,
                database_name=temporary_name,
                backup_bytes=target_backup.read_bytes(),
            )
            result["alembic_version"] = verify_postgresql_restore(
                compose_project_name=compose_project_name,
                database_name=temporary_name,
            )
            result["temporary_restore_database"] = temporary_name
        finally:
            drop_postgresql_restore_database(
                compose_project_name=compose_project_name,
                database_name=temporary_name,
            )
        result["temporary_database_removed"] = True
    return result


def run_demo(arguments: argparse.Namespace) -> dict[str, Any]:
    """根据已解析参数执行对应数据库演示并写入结果文件。

    Args:
        arguments: argparse 返回的受控命令行参数。

    Returns:
        含后端、操作和验证详情的完整摘要。
    """
    work_directory = resolve_work_directory(arguments.work_directory)
    if arguments.backend == "sqlite":
        details = run_sqlite_demo(
            action=arguments.action,
            work_directory=work_directory,
            database_path=arguments.database_path,
            backup_path=arguments.backup_path,
            restore_target=arguments.restore_target,
            confirm_restore=arguments.confirm_restore,
        )
    else:
        details = run_postgresql_demo(
            action=arguments.action,
            work_directory=work_directory,
            backup_path=arguments.backup_path,
            restore_database_name=arguments.restore_database_name,
            confirm_restore=arguments.confirm_restore,
            compose_project_name=arguments.compose_project_name,
        )
    summary = {
        "schema_version": "1.0",
        "release_version": "1.0.0",
        "backend": arguments.backend,
        "details": details,
    }
    result_path = work_directory / "result.json"
    result_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {**summary, "result_path": str(result_path)}


def main(argv: Sequence[str] | None = None) -> int:
    """解析参数、执行备份恢复演示并输出 JSON 摘要。

    Args:
        argv: 可选命令行参数序列；省略时读取当前进程参数。

    Returns:
        验证成功时返回零；参数或运行错误时由 argparse 返回非零。
    """
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    try:
        summary = run_demo(arguments)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
