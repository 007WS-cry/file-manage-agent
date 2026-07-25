from __future__ import annotations

from pathlib import Path

from app.runtime.dispatcher import create_background_submission
from app.runtime.job_queue import JobQueue
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本文件单元测试后台任务的持久化入队、事务领取、状态查询和正常收口。"""


def create_runtime_database(database_path: Path) -> None:
    """使用 ORM 元数据创建当前单元测试独占的十表数据库。

    Args:
        database_path: 等待创建的临时 SQLite 文件。
    """
    engine = create_application_engine(database_path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def build_submission_payload(tmp_path: Path) -> dict:
    """构造不启用真实模型且只访问临时空输入目录的后台请求。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        可由运行时 dispatcher 规范化的治理请求信封。
    """
    input_root = tmp_path / "input"
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
            "artifact_root": str(tmp_path / "artifacts"),
            "report_root": str(tmp_path / "reports"),
        },
    }


def test_background_job_can_enqueue_claim_run_and_complete(tmp_path: Path) -> None:
    """后台任务应在四个短事务中完成入队、领取、执行和最终收口。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    queue = JobQueue(database_path)
    try:
        queued = create_background_submission(
            build_submission_payload(tmp_path),
            queue=queue,
            checkpoint_path=checkpoint_path,
            max_attempts=3,
        )

        assert queued["status"] == "queued"
        assert queued["attempt_count"] == 0
        assert queued["current_worker_id"] is None
        assert queue.get_run(queued["run_id"])["status"] == "queued"

        claimed = queue.claim("worker-unit", lease_seconds=30.0)

        assert claimed is not None
        assert claimed["id"] == queued["id"]
        assert claimed["status"] == "leased"
        assert claimed["attempt_count"] == 1
        assert claimed["current_worker_id"] == "worker-unit"

        running = queue.mark_running(
            queued["id"],
            worker_id="worker-unit",
        )
        assert running["status"] == "running"

        completed = queue.finish(
            queued["id"],
            worker_id="worker-unit",
            status="completed",
            report_path=str(tmp_path / "reports" / "result.md"),
        )

        assert completed["status"] == "completed"
        assert completed["current_worker_id"] is None
        assert completed["finished_at"] is not None
        assert queue.get_job_by_run_id(queued["run_id"])["id"] == queued["id"]
        assert queue.get_run(queued["run_id"])["status"] == "completed"
    finally:
        queue.close()


def test_queue_returns_none_when_no_job_is_claimable(tmp_path: Path) -> None:
    """空队列不得创建租约或伪造后台任务。"""
    database_path = tmp_path / "application.sqlite3"
    create_runtime_database(database_path)
    queue = JobQueue(database_path)
    try:
        assert queue.claim("worker-empty", lease_seconds=30.0) is None
    finally:
        queue.close()
