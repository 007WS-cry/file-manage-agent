from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.runtime.dispatcher import create_background_submission
from app.runtime.job_queue import JobQueue
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.orm_models import Base
from app.storage.repositories import create_repository_bundle

"""本文件集成测试 Worker 异常退出后的租约过期、重新领取和尝试耗尽语义。"""


def create_runtime_database(database_path: Path) -> None:
    """创建当前租约恢复测试独占的十表 SQLite 数据库。

    Args:
        database_path: 等待创建的临时应用数据库路径。
    """
    engine = create_application_engine(database_path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def build_submission_payload(tmp_path: Path, *, suffix: str) -> dict:
    """构造使用独立输入和产物目录的后台请求。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。
        suffix: 用于隔离多次提交目录的测试后缀。

    Returns:
        可安全写入后台任务 JSON 列的治理请求信封。
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


def test_expired_lease_requeues_job_for_another_worker(tmp_path: Path) -> None:
    """未耗尽尝试次数的运行中任务应在租约过期后被其他 Worker 重新领取。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    queue = JobQueue(database_path)
    try:
        job = create_background_submission(
            build_submission_payload(tmp_path, suffix="requeue"),
            queue=queue,
            checkpoint_path=checkpoint_path,
            max_attempts=2,
        )
        first_claim_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        first_claim = queue.claim(
            "worker-crashed",
            lease_seconds=5.0,
            now=first_claim_at,
        )
        assert first_claim is not None
        queue.mark_running(
            job["id"],
            worker_id="worker-crashed",
            now=first_claim_at,
        )

        recovered = queue.requeue_expired(
            now=first_claim_at + timedelta(seconds=6),
        )

        assert recovered == 1
        requeued = queue.get_job(job["id"])
        assert requeued is not None
        assert requeued["status"] == "queued"
        assert requeued["current_worker_id"] is None
        second_claim = queue.claim(
            "worker-replacement",
            lease_seconds=5.0,
            now=first_claim_at + timedelta(seconds=7),
        )
        assert second_claim is not None
        assert second_claim["attempt_count"] == 2
        assert second_claim["current_worker_id"] == "worker-replacement"
    finally:
        queue.close()

    engine = create_application_engine(database_path)
    session_factory = create_session_factory(engine)
    try:
        with open_application_session(session_factory) as session:
            lease = create_repository_bundle(session).worker_leases.get(job["id"])
            assert lease is not None
            assert lease.status == "active"
            assert lease.worker_id == "worker-replacement"
    finally:
        engine.dispose()


def test_expired_lease_fails_job_after_attempt_limit(tmp_path: Path) -> None:
    """已经耗尽最大尝试次数的过期任务应进入 failed 而不是无限重入队。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    queue = JobQueue(database_path)
    try:
        job = create_background_submission(
            build_submission_payload(tmp_path, suffix="failed"),
            queue=queue,
            checkpoint_path=checkpoint_path,
            max_attempts=1,
        )
        claimed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        assert queue.claim(
            "worker-only",
            lease_seconds=5.0,
            now=claimed_at,
        )
        queue.mark_running(
            job["id"],
            worker_id="worker-only",
            now=claimed_at,
        )

        recovered = queue.requeue_expired(
            now=claimed_at + timedelta(seconds=6),
        )

        assert recovered == 1
        failed = queue.get_job(job["id"])
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["finished_at"] is not None
        assert queue.get_run(job["run_id"])["status"] == "failed"
        assert queue.claim(
            "worker-late",
            lease_seconds=5.0,
            now=claimed_at + timedelta(seconds=7),
        ) is None
    finally:
        queue.close()
