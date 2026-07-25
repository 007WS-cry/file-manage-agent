from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.runtime.dispatcher import create_background_submission
from app.runtime.job_queue import JobQueue
from app.runtime.worker import BackgroundWorker
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本文件集成测试 Worker 服务收口语义以及真实独立 Python 进程执行后台治理图。"""


# 当前仓库根目录，用于独立 Worker 子进程继承源码工作目录。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def create_runtime_database(database_path: Path) -> None:
    """创建 Background Worker 集成测试独占的十表数据库。

    Args:
        database_path: 等待创建的临时 SQLite 文件。
    """
    engine = create_application_engine(database_path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def build_submission_payload(tmp_path: Path, *, suffix: str) -> dict:
    """构造不启用真实模型的后台治理请求信封。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。
        suffix: 用于隔离输入、产物和报告目录的后缀。

    Returns:
        可由 API dispatcher 持久化的请求信封。
    """
    input_root = tmp_path / f"input-{suffix}"
    input_root.mkdir()
    return {
        "request": {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 10,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": False,
        },
        "workspace": {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(tmp_path / f"artifacts-{suffix}"),
            "report_root": str(tmp_path / f"reports-{suffix}"),
        },
    }


def test_worker_service_completes_claimed_job_with_injected_executor(tmp_path: Path) -> None:
    """Worker 应把执行器返回的 completed 图状态原子映射到队列和运行记录。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    queue = JobQueue(database_path)
    try:
        job = create_background_submission(
            build_submission_payload(tmp_path, suffix="injected"),
            queue=queue,
            checkpoint_path=checkpoint_path,
        )
        worker = BackgroundWorker(
            queue,
            worker_id="worker-injected",
            lease_seconds=10.0,
            heartbeat_interval_seconds=2.0,
            job_executor=lambda _: {
                "run": {"status": "completed"},
                "report": {"report_path": str(tmp_path / "report.md")},
            },
        )

        assert worker.run_once() is True

        completed = queue.get_job(job["id"])
        assert completed is not None
        assert completed["status"] == "completed"
        assert completed["report_path"] == str(tmp_path / "report.md")
        assert queue.get_run(job["run_id"])["status"] == "completed"
    finally:
        queue.close()


def test_worker_entrypoint_executes_real_graph_in_another_process(tmp_path: Path) -> None:
    """独立 Python Worker 进程应使用共享 SQLite 完成一个真实无数据治理运行。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    queue = JobQueue(database_path)
    try:
        job = create_background_submission(
            build_submission_payload(tmp_path, suffix="process"),
            queue=queue,
            checkpoint_path=checkpoint_path,
            max_attempts=1,
        )
    finally:
        queue.close()

    completed_process = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.entrypoints.worker",
            "--database-path",
            str(database_path),
            "--worker-id",
            "worker-subprocess",
            "--lease-seconds",
            "30",
            "--heartbeat-interval",
            "5",
            "--once",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed_process.returncode == 0, completed_process.stderr
    assert '"processed": true' in completed_process.stdout.lower()
    queue = JobQueue(database_path)
    try:
        completed = queue.get_job(job["id"])
        assert completed is not None
        assert completed["status"] == "completed"
        assert completed["current_worker_id"] is None
        assert queue.get_run(job["run_id"])["status"] == "completed"
    finally:
        queue.close()
