from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from alembic import command
from app.api.app import create_app
from app.runtime.dispatcher import execute_foreground_submission
from app.runtime.job_queue import JobQueue
from app.runtime.worker import BackgroundWorker
from app.storage.database import (
    APPLICATION_DATABASE_PATH_ENV,
    APPLICATION_DATABASE_URL_ENV,
    ApplicationDatabaseTarget,
)

"""本脚本迁移演示数据库并验收 Python 前台、API 后台、Cron、报告和输入只读契约。"""


# 当前仓库根目录，用于稳定定位 Alembic 配置。
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# PostgreSQL 演示只允许连接 Docker 暴露到本机或 Compose 内部服务名。
LOCAL_POSTGRESQL_HOSTS = frozenset({"127.0.0.1", "localhost", "postgres"})

# 演示运行支持的固定链路名称。
DEMO_MODES = ("foreground", "background", "cron", "all")


def sha256_file(path: Path) -> str:
    """流式计算演示输入普通文件的 SHA-256。

    Args:
        path: 等待计算摘要的普通文件。

    Returns:
        小写十六进制 SHA-256 字符串。
    """
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_input_manifest(input_root: Path) -> dict[str, Any]:
    """扫描输入目录并构造路径、大小和 SHA-256 清单。

    Args:
        input_root: 只读业务输入根目录。

    Returns:
        可与数据生成器基线直接比较的清单对象。
    """
    files = []
    for path in sorted(item for item in input_root.rglob("*") if item.is_file()):
        files.append(
            {
                "relative_path": path.relative_to(input_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return {
        "schema_version": "1.0",
        "input_root": "input",
        "file_count": len(files),
        "files": files,
    }


def write_json(path: Path, payload: object) -> None:
    """以 UTF-8 和稳定缩进写入演示结果 JSON。

    Args:
        path: JSON 输出路径。
        payload: 可以由标准库 JSON 编码的数据。
    """
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """构建端到端演示运行器的命令行参数解析器。

    Returns:
        包含演示根目录、数据库后端和运行链路参数的解析器。
    """
    parser = argparse.ArgumentParser(
        description="运行 1.0.2 前台、后台与 Cron 端到端演示并复核输入不变性。"
    )
    parser.add_argument(
        "--demo-root",
        type=Path,
        default=Path(".artifacts/demo"),
        help="generate_demo_data.py 已生成的演示根目录。",
    )
    parser.add_argument(
        "--database-backend",
        choices=("sqlite", "postgresql"),
        default="sqlite",
        help="应用数据库后端；PostgreSQL 仅接受本机 Docker 拓扑。",
    )
    parser.add_argument(
        "--database-url-env",
        default=APPLICATION_DATABASE_URL_ENV,
        help="PostgreSQL SQLAlchemy URL 所在的环境变量名称。",
    )
    parser.add_argument(
        "--mode",
        choices=DEMO_MODES,
        default="all",
        help="只运行一条链路，或按前台、后台、Cron 顺序运行全部链路。",
    )
    return parser


def load_json_object(path: Path) -> dict[str, Any]:
    """读取受控演示目录中的 UTF-8 JSON 对象。

    Args:
        path: 等待读取的 JSON 普通文件路径。

    Returns:
        JSON 顶层对象。

    Raises:
        ValueError: 文件缺失、不是普通文件或顶层不是对象时抛出。
    """
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.suffix.lower() != ".json":
        raise ValueError(f"演示配置必须是 JSON 普通文件：{resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"演示配置顶层必须是对象：{resolved}")
    return payload


def resolve_demo_root(demo_root: Path) -> Path:
    """校验演示根目录及其只读输入目录。

    Args:
        demo_root: 用户提供的演示目录。

    Returns:
        通过校验的绝对演示根目录。

    Raises:
        ValueError: 根目录或输入目录缺失、不是目录或使用符号链接时抛出。
    """
    candidate = demo_root.expanduser()
    if candidate.is_symlink():
        raise ValueError("演示根目录不得是符号链接")
    resolved = candidate.resolve(strict=True)
    input_root = resolved / "input"
    if not resolved.is_dir() or not input_root.is_dir() or input_root.is_symlink():
        raise ValueError("演示根目录必须包含非符号链接 input 目录")
    return resolved


def resolve_database_target(
    demo_root: Path,
    *,
    backend: Literal["sqlite", "postgresql"],
    database_url_env: str,
) -> ApplicationDatabaseTarget:
    """解析 SQLite 演示文件或仅限本机 Docker 的 PostgreSQL URL。

    Args:
        demo_root: 已验证的演示根目录。
        backend: ``sqlite`` 或 ``postgresql``。
        database_url_env: PostgreSQL URL 所在环境变量名称。

    Returns:
        可交给应用数据库和迁移层的连接目标。

    Raises:
        ValueError: PostgreSQL URL 缺失、后端不符或指向远程主机时抛出。
    """
    if backend == "sqlite":
        return demo_root / "database" / "file-governance-app.sqlite3"
    if not database_url_env or not database_url_env.strip():
        raise ValueError("database_url_env 不得为空")
    database_url = os.environ.get(database_url_env, "").strip()
    if not database_url:
        raise ValueError(f"PostgreSQL 演示需要环境变量 {database_url_env}")
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError("PostgreSQL 演示 URL 必须使用 postgresql scheme")
    if parsed.host not in LOCAL_POSTGRESQL_HOSTS:
        raise ValueError("PostgreSQL 演示只允许连接本机 Docker 或 postgres 服务")
    return database_url


@contextmanager
def migration_environment(
    database_target: ApplicationDatabaseTarget,
) -> Iterator[None]:
    """临时设置 Alembic 数据库环境变量并在迁移后恢复进程环境。

    Args:
        database_target: SQLite 路径或 PostgreSQL SQLAlchemy URL。

    Yields:
        Alembic 可以安全读取目标变量期间的控制权。
    """
    previous_url = os.environ.get(APPLICATION_DATABASE_URL_ENV)
    previous_path = os.environ.get(APPLICATION_DATABASE_PATH_ENV)
    try:
        if isinstance(database_target, str) and "://" in database_target:
            os.environ[APPLICATION_DATABASE_URL_ENV] = database_target
            os.environ.pop(APPLICATION_DATABASE_PATH_ENV, None)
        else:
            os.environ[APPLICATION_DATABASE_PATH_ENV] = str(database_target)
            os.environ.pop(APPLICATION_DATABASE_URL_ENV, None)
        yield
    finally:
        if previous_url is None:
            os.environ.pop(APPLICATION_DATABASE_URL_ENV, None)
        else:
            os.environ[APPLICATION_DATABASE_URL_ENV] = previous_url
        if previous_path is None:
            os.environ.pop(APPLICATION_DATABASE_PATH_ENV, None)
        else:
            os.environ[APPLICATION_DATABASE_PATH_ENV] = previous_path


def upgrade_database(database_target: ApplicationDatabaseTarget) -> None:
    """把演示应用数据库升级到当前 Alembic head。

    Args:
        database_target: SQLite 路径或本机 Docker PostgreSQL URL。
    """
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    with migration_environment(database_target):
        command.upgrade(alembic_config, "head")


def assert_input_manifest_unchanged(
    demo_root: Path,
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    """比较运行前后的文件数量、相对路径、大小和 SHA-256。

    Args:
        demo_root: 当前演示根目录。
        baseline: generate_demo_data.py 保存的基线清单。

    Returns:
        与基线完全一致的最新输入清单。

    Raises:
        RuntimeError: 输入清单发生任意变化时抛出。
    """
    current = build_input_manifest(demo_root / "input")
    comparable_baseline = {
        "schema_version": baseline.get("schema_version"),
        "input_root": baseline.get("input_root"),
        "file_count": baseline.get("file_count"),
        "files": baseline.get("files"),
    }
    if current != comparable_baseline:
        raise RuntimeError("治理运行改变了输入文件数量、路径、大小或 SHA-256")
    return current


def execute_foreground_demo(
    envelope: Mapping[str, Any],
    *,
    database_target: ApplicationDatabaseTarget,
    demo_root: Path,
) -> dict[str, Any]:
    """通过受信任 Python 入口同步运行一次前台治理。

    Args:
        envelope: 演示请求信封。
        database_target: 已迁移的应用数据库目标。
        demo_root: 当前演示根目录。

    Returns:
        前台运行状态和报告路径摘要。

    Raises:
        RuntimeError: 图未完成或没有生成报告时抛出。
    """
    result = execute_foreground_submission(
        envelope,
        application_database_path=database_target,
        checkpoint_path=demo_root / "checkpoints" / "python-foreground.sqlite3",
    )
    run = result.get("run")
    report = result.get("report")
    if not isinstance(run, Mapping) or run.get("status") not in {"completed", "partial"}:
        raise RuntimeError("Python 前台演示未收口")
    if not isinstance(report, Mapping) or not report.get("report_path"):
        raise RuntimeError("Python 前台演示没有生成报告")
    return {
        "status": run["status"],
        "run_id": run["run_id"],
        "report_path": str(report["report_path"]),
    }


def create_demo_application(
    database_target: ApplicationDatabaseTarget,
    *,
    checkpoint_path: Path,
):
    """按数据库目标创建演示 FastAPI 应用。

    Args:
        database_target: SQLite 路径或 PostgreSQL URL。
        checkpoint_path: 后台 Worker 共用的 SQLite checkpoint。

    Returns:
        已配置运行数据库和 checkpoint 的 FastAPI 应用。
    """
    if isinstance(database_target, str) and "://" in database_target:
        return create_app(
            database_url=database_target,
            checkpoint_path=checkpoint_path,
        )
    return create_app(
        database_path=database_target,
        checkpoint_path=checkpoint_path,
    )


def execute_api_background_demo(
    envelope: Mapping[str, Any],
    *,
    database_target: ApplicationDatabaseTarget,
    demo_root: Path,
) -> dict[str, Any]:
    """通过 FastAPI 提交、Worker 执行并下载后台报告。

    Args:
        envelope: 演示请求信封。
        database_target: 已迁移的应用数据库目标。
        demo_root: 当前演示根目录。

    Returns:
        后台运行、任务和报告下载摘要。

    Raises:
        RuntimeError: API、Worker 或报告下载任一环节失败时抛出。
    """
    application = create_demo_application(
        database_target,
        checkpoint_path=demo_root / "checkpoints" / "background.sqlite3",
    )
    with TestClient(application) as client:
        response = client.post(
            "/runs",
            json={
                "execution_mode": "background",
                "max_attempts": 3,
                "payload": dict(envelope),
            },
        )
        if response.status_code != 202:
            raise RuntimeError(f"后台 API 提交失败：{response.text}")
        receipt = response.json()
        queue = JobQueue(database_target)
        try:
            worker = BackgroundWorker(
                queue,
                worker_id="demo-background-worker",
                lease_seconds=30.0,
                heartbeat_interval_seconds=5.0,
            )
            if not worker.run_once():
                raise RuntimeError("后台 Worker 未领取演示任务")
        finally:
            queue.close()
        status_response = client.get(f"/runs/{receipt['run_id']}")
        report_response = client.get(f"/runs/{receipt['run_id']}/report")
        if status_response.status_code != 200:
            raise RuntimeError(f"后台状态查询失败：{status_response.text}")
        if report_response.status_code != 200:
            raise RuntimeError(f"后台报告下载失败：{report_response.text}")
        status_payload = status_response.json()
        output_path = demo_root / "e2e-demo-output" / "background-report.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(report_response.content)
        return {
            "status": status_payload["status"],
            "run_id": receipt["run_id"],
            "job_id": receipt["job_id"],
            "report_download_path": str(output_path),
        }


def execute_cron_demo(
    envelope: Mapping[str, Any],
    *,
    database_target: ApplicationDatabaseTarget,
    demo_root: Path,
) -> dict[str, Any]:
    """通过计划 API、Cron 入队、Worker 和报告 API 完成治理链路。

    Args:
        envelope: 演示请求信封。
        database_target: 已迁移的应用数据库目标。
        demo_root: 当前演示根目录。

    Returns:
        Cron 计划、运行、任务和报告下载摘要。

    Raises:
        RuntimeError: 计划创建、入队、Worker 或报告下载失败时抛出。
    """
    application = create_demo_application(
        database_target,
        checkpoint_path=demo_root / "checkpoints" / "cron.sqlite3",
    )
    with TestClient(application) as client:
        schedule_response = client.post(
            "/schedules",
            json={
                "name": "1.0.2 端到端演示",
                "cron_expression": "0 2 * * *",
                "timezone": "Asia/Shanghai",
                "enabled": True,
                "payload": dict(envelope),
            },
        )
        if schedule_response.status_code != 201:
            raise RuntimeError(f"Cron 计划创建失败：{schedule_response.text}")
        schedule = schedule_response.json()
        job = application.state.scheduler_service.enqueue_schedule(
            schedule["schedule_id"],
            triggered_at=datetime.now(timezone.utc),
        )
        if job is None:
            raise RuntimeError("Cron 计划未创建后台任务")
        queue = JobQueue(database_target)
        try:
            worker = BackgroundWorker(
                queue,
                worker_id="demo-cron-worker",
                lease_seconds=30.0,
                heartbeat_interval_seconds=5.0,
            )
            if not worker.run_once():
                raise RuntimeError("后台 Worker 未领取 Cron 任务")
        finally:
            queue.close()
        report_response = client.get(f"/runs/{job['run_id']}/report")
        if report_response.status_code != 200:
            raise RuntimeError(f"Cron 报告下载失败：{report_response.text}")
        output_path = demo_root / "e2e-demo-output" / "cron-report.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(report_response.content)
        return {
            "status": "completed",
            "schedule_id": schedule["schedule_id"],
            "run_id": job["run_id"],
            "job_id": job["id"],
            "report_download_path": str(output_path),
        }


def run_demo(
    demo_root: Path,
    *,
    database_backend: Literal["sqlite", "postgresql"],
    database_url_env: str,
    mode: Literal["foreground", "background", "cron", "all"],
) -> dict[str, Any]:
    """迁移数据库、执行选定链路并验证只读输入契约。

    Args:
        demo_root: generate_demo_data.py 生成的演示目录。
        database_backend: SQLite 或本机 Docker PostgreSQL。
        database_url_env: PostgreSQL URL 所在环境变量名称。
        mode: 需要执行的单条链路或全部链路。

    Returns:
        版本、数据库、各链路结果和输入不变性摘要。
    """
    resolved_root = resolve_demo_root(demo_root)
    envelope = load_json_object(resolved_root / "request.json")
    baseline = load_json_object(resolved_root / "manifest.before.json")
    database_target = resolve_database_target(
        resolved_root,
        backend=database_backend,
        database_url_env=database_url_env,
    )
    upgrade_database(database_target)
    selected_modes = (
        ("foreground", "background", "cron") if mode == "all" else (mode,)
    )
    results: dict[str, Any] = {}
    for selected_mode in selected_modes:
        if selected_mode == "foreground":
            results[selected_mode] = execute_foreground_demo(
                envelope,
                database_target=database_target,
                demo_root=resolved_root,
            )
        elif selected_mode == "background":
            results[selected_mode] = execute_api_background_demo(
                envelope,
                database_target=database_target,
                demo_root=resolved_root,
            )
        else:
            results[selected_mode] = execute_cron_demo(
                envelope,
                database_target=database_target,
                demo_root=resolved_root,
            )
    current_manifest = assert_input_manifest_unchanged(resolved_root, baseline)
    summary = {
        "schema_version": "1.0",
        "release_version": "1.0.2",
        "database_backend": database_backend,
        "mode": mode,
        "results": results,
        "readonly_input": {
            "unchanged": True,
            "file_count": current_manifest["file_count"],
        },
    }
    output_path = resolved_root / "e2e-demo-output" / "result.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, summary)
    return {**summary, "result_path": str(output_path)}


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行并运行端到端演示。

    Args:
        argv: 可选命令行参数序列；省略时读取当前进程参数。

    Returns:
        所有验收通过时返回零；校验或运行失败时由 argparse 返回非零。
    """
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    try:
        summary = run_demo(
            arguments.demo_root,
            database_backend=arguments.database_backend,
            database_url_env=arguments.database_url_env,
            mode=arguments.mode,
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
