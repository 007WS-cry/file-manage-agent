from __future__ import annotations

from pathlib import Path

import pytest

from app.runtime.dispatcher import create_background_submission
from app.runtime.job_queue import JobQueue
from app.state.models import PendingInterruptState
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本文件单元测试后台中断持久化、幂等恢复和独立恢复计数。"""


def create_runtime_database(database_path: Path) -> None:
    """创建后台恢复 Repository 测试独占的应用数据库。

    Args:
        database_path: 等待创建的临时 SQLite 文件。
    """
    engine = create_application_engine(database_path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def build_submission_payload(tmp_path: Path) -> dict:
    """构造后台恢复 Repository 测试使用的最小请求信封。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        可由 dispatcher 规范化并持久化的请求信封。
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


def test_resume_is_idempotent_and_does_not_consume_exception_attempts(
    tmp_path: Path,
) -> None:
    """正常人工恢复应保持 attempt_count，并以独立 resume_count 记录应用次数。"""
    database_path = tmp_path / "application.sqlite3"
    create_runtime_database(database_path)
    queue = JobQueue(database_path)
    try:
        create_background_submission(
            build_submission_payload(tmp_path),
            queue=queue,
            checkpoint_path=tmp_path / "checkpoint.sqlite3",
        )
        claimed = queue.claim("worker-resume-unit", lease_seconds=30.0)
        assert claimed is not None
        queue.mark_running(claimed["id"], worker_id="worker-resume-unit")
        pending = PendingInterruptState(
            interrupt_id="interrupt-current",
            kind="error_recovery",
            payload={
                "kind": "error_recovery",
                "allowed_actions": ["abort"],
            },
            created_at="2026-07-26T08:00:00+00:00",
        )
        waiting = queue.finish(
            claimed["id"],
            worker_id="worker-resume-unit",
            status="waiting_human",
            pending_interrupt=pending,
        )

        assert waiting["attempt_count"] == 1
        assert waiting["resume_count"] == 0
        with pytest.raises(RuntimeError, match="interrupt_id"):
            queue.enqueue_resume(
                waiting["run_id"],
                request_id="resume-stale",
                interrupt_id="interrupt-expired",
                kind="error_recovery",
                value={"action": "abort"},
            )

        first = queue.enqueue_resume(
            waiting["run_id"],
            request_id="resume-001",
            interrupt_id="interrupt-current",
            kind="error_recovery",
            value={"action": "abort"},
        )
        duplicate = queue.enqueue_resume(
            waiting["run_id"],
            request_id="resume-001",
            interrupt_id="interrupt-current",
            kind="error_recovery",
            value={"action": "abort"},
        )

        assert first["status"] == "resume_queued"
        assert duplicate == first
        resumed_claim = queue.claim("worker-resume-unit", lease_seconds=30.0)
        assert resumed_claim is not None
        assert resumed_claim["attempt_count"] == 1
        queue.mark_running(resumed_claim["id"], worker_id="worker-resume-unit")
        completed = queue.finish(
            resumed_claim["id"],
            worker_id="worker-resume-unit",
            status="completed",
        )

        assert completed["attempt_count"] == 1
        assert completed["resume_count"] == 1
        assert completed["pending_interrupt"] is None
        assert completed["resume"]["status"] == "applied"
        assert completed["resume"]["value"] is None

        applied_duplicate = queue.enqueue_resume(
            waiting["run_id"],
            request_id="resume-001",
            interrupt_id="interrupt-current",
            kind="error_recovery",
            value={"action": "abort"},
        )
        assert applied_duplicate["status"] == "completed"
        assert applied_duplicate["resume_count"] == 1
    finally:
        queue.close()
