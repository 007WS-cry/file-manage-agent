from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.entrypoints.cli import main as cli_main
from app.runtime.dispatcher import execute_foreground_submission
from app.runtime.job_queue import JobQueue
from app.runtime.worker import BackgroundWorker
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本文件端到端验收 Python/CLI 前台、API 后台、Cron 入队、Worker 和报告下载链路。"""


def create_runtime_database(database_path: Path) -> None:
    """创建当前端到端场景独占的应用数据库。

    Args:
        database_path: 等待初始化的临时 SQLite 文件。
    """
    engine = create_application_engine(database_path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def build_empty_envelope(tmp_path: Path) -> dict:
    """构造无需模型和人工确认的空目录治理请求。

    Args:
        tmp_path: pytest 当前测试的临时目录。

    Returns:
        前台、后台和 Cron 可共用的完整请求信封。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    return {
        "request": {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
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
        "prompt": {"enabled": False},
        "hooks": {"enabled": False},
        "llm": {"enabled": False},
        "email_mcp": {"enabled": False},
    }


def test_python_and_cli_foreground_complete_with_reports(
    tmp_path: Path,
    capsys,
) -> None:
    """Python 与 CLI 前台入口应完成运行并只公开各自约定的结果。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "python-checkpoint.sqlite3"
    create_runtime_database(database_path)
    envelope = build_empty_envelope(tmp_path)

    python_result = execute_foreground_submission(
        envelope,
        application_database_path=database_path,
        checkpoint_path=checkpoint_path,
    )

    assert python_result["run"]["status"] == "completed"
    assert Path(python_result["report"]["report_path"]).is_file()
    request_path = tmp_path / "cli-request.json"
    cli_payload = {
        **envelope,
        "workspace": {
            **envelope["workspace"],
            "artifact_root": str(tmp_path / "cli-artifacts"),
            "report_root": str(tmp_path / "cli-reports"),
        },
        "checkpoint": {"backend": "memory"},
    }
    request_path.write_text(
        json.dumps(cli_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "run",
            str(request_path),
            "--thread-id",
            "e2e-cli-foreground",
        ]
    )
    captured = capsys.readouterr()
    cli_result = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert cli_result["status"] == "completed"
    assert Path(cli_result["report_path"]).is_file()
    assert "documents" not in cli_result
    assert "report_markdown" not in cli_result


def test_api_background_worker_and_report_download_complete(tmp_path: Path) -> None:
    """API 后台提交应由独立 Worker 收口并通过受控接口下载报告。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "background-checkpoint.sqlite3"
    create_runtime_database(database_path)
    envelope = build_empty_envelope(tmp_path)
    application = create_app(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
    )

    with TestClient(application) as client:
        response = client.post(
            "/runs",
            json={
                "execution_mode": "background",
                "max_attempts": 3,
                "payload": envelope,
            },
        )
        assert response.status_code == 202
        receipt = response.json()
        queue = JobQueue(database_path)
        try:
            worker = BackgroundWorker(
                queue,
                worker_id="e2e-api-worker",
                lease_seconds=30.0,
                heartbeat_interval_seconds=5.0,
            )
            assert worker.run_once() is True
        finally:
            queue.close()

        status_response = client.get(f"/runs/{receipt['run_id']}")
        report_response = client.get(f"/runs/{receipt['run_id']}/report")

    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["status"] == "completed"
    assert status_payload["report_available"] is True
    assert report_response.status_code == 200
    assert report_response.content.startswith(b"# ")


def test_cron_enqueue_reaches_worker_and_report_api(tmp_path: Path) -> None:
    """持久化 Cron 计划应只入队，并最终经 Worker 产生可下载报告。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "cron-checkpoint.sqlite3"
    create_runtime_database(database_path)
    envelope = build_empty_envelope(tmp_path)
    application = create_app(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
    )

    with TestClient(application) as client:
        schedule_response = client.post(
            "/schedules",
            json={
                "name": "端到端 Cron",
                "cron_expression": "0 2 * * *",
                "timezone": "Asia/Shanghai",
                "enabled": True,
                "payload": envelope,
            },
        )
        assert schedule_response.status_code == 201
        schedule_id = schedule_response.json()["schedule_id"]
        job = application.state.scheduler_service.enqueue_schedule(
            schedule_id,
            triggered_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        )
        assert job is not None
        assert job["trigger_source"] == "cron"
        assert job["status"] == "queued"

        queue = JobQueue(database_path)
        try:
            worker = BackgroundWorker(
                queue,
                worker_id="e2e-cron-worker",
                lease_seconds=30.0,
                heartbeat_interval_seconds=5.0,
            )
            assert worker.run_once() is True
        finally:
            queue.close()

        report_response = client.get(f"/runs/{job['run_id']}/report")
        schedule_after = client.get(f"/schedules/{schedule_id}").json()

    assert report_response.status_code == 200
    assert schedule_after["last_run_id"] == job["run_id"]
