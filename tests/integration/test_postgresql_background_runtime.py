from __future__ import annotations

from pathlib import Path

from app.runtime.dispatcher import create_background_submission
from app.runtime.job_queue import JobQueue
from app.runtime.worker import BackgroundWorker

"""本文件验证 PostgreSQL 队列可以完成后台提交、领取、执行与最终状态收口。"""


def _build_submission_payload(tmp_path: Path) -> dict:
    """构造不启用真实模型、适合 PostgreSQL 后台运行测试的请求信封。

    Args:
        tmp_path: pytest 为当前测试提供的临时工作目录。

    Returns:
        只包含路径和确定性治理参数的后台请求信封。
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


def test_postgresql_background_worker_completes_without_persisting_password(
    postgresql_database_url: str,
    tmp_path: Path,
) -> None:
    """后台任务应在 PostgreSQL 收口，且请求 JSON 只能保存固定环境变量引用。"""
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    queue = JobQueue(postgresql_database_url)
    try:
        job = create_background_submission(
            _build_submission_payload(tmp_path),
            queue=queue,
            checkpoint_path=checkpoint_path,
        )
        serialized_payload = str(job["request_payload"])
        assert "integration-test-password" not in serialized_payload
        assert job["request_payload"]["application_database"] == {
            "enabled": True,
            "backend": "postgresql",
            "database_path": None,
            "database_url_env": "FILE_GOVERNANCE_DATABASE_URL",
        }

        report_path = tmp_path / "reports" / "postgresql-report.md"
        worker = BackgroundWorker(
            queue,
            worker_id="postgres-background-worker",
            lease_seconds=10.0,
            heartbeat_interval_seconds=2.0,
            job_executor=lambda _: {
                "run": {"status": "completed"},
                "report": {"report_path": str(report_path)},
            },
        )
        assert worker.run_once() is True

        completed = queue.get_job(job["id"])
        assert completed is not None
        assert completed["status"] == "completed"
        assert completed["attempt_count"] == 1
        assert queue.get_run(job["run_id"])["status"] == "completed"
    finally:
        queue.close()
