from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import __version__
from app.api.app import create_app
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本文件集成测试 HTTP API 的后台立即提交、状态查询和响应字段隔离。"""


def create_runtime_database(database_path: Path) -> None:
    """创建 API 集成测试独占的十表应用数据库。

    Args:
        database_path: 等待创建的临时 SQLite 文件。
    """
    engine = create_application_engine(database_path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def build_api_submission(tmp_path: Path) -> dict:
    """构造 API 后台提交测试使用的完整 JSON 请求。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        满足 RunSubmissionRequest 的后台提交对象。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    return {
        "execution_mode": "background",
        "max_attempts": 3,
        "payload": {
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
        },
    }


def test_api_submits_immediately_and_queries_by_run_and_job_id(tmp_path: Path) -> None:
    """POST 应立即返回 queued，两个 GET 接口应只返回脱敏状态白名单。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    application = create_app(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
    )

    with TestClient(application) as client:
        health = client.get("/health")
        response = client.post("/runs", json=build_api_submission(tmp_path))

        assert health.status_code == 200
        assert health.json() == {"status": "ok", "version": __version__}
        assert response.status_code == 202
        receipt = response.json()
        assert receipt["status"] == "queued"
        assert receipt["run_id"]
        assert receipt["job_id"]
        assert receipt["thread_id"]

        run_response = client.get(f"/runs/{receipt['run_id']}")
        job_response = client.get(f"/runs/jobs/{receipt['job_id']}")

        assert run_response.status_code == 200
        assert run_response.json()["status"] == "queued"
        assert run_response.json()["background_job"]["job_id"] == receipt["job_id"]
        assert job_response.status_code == 200
        assert job_response.json()["status"] == "queued"
        assert "request_payload" not in job_response.json()


def test_api_rejects_unknown_or_invalid_submission_fields(tmp_path: Path) -> None:
    """API 应拒绝未知顶层字段和缺少 request/workspace 的信封。"""
    database_path = tmp_path / "application.sqlite3"
    create_runtime_database(database_path)
    application = create_app(
        database_path=database_path,
        checkpoint_path=tmp_path / "checkpoint.sqlite3",
    )

    with TestClient(application) as client:
        unknown = client.post(
            "/runs",
            json={
                "execution_mode": "background",
                "max_attempts": 3,
                "payload": {},
                "unexpected": True,
            },
        )
        invalid = client.post(
            "/runs",
            json={
                "execution_mode": "background",
                "max_attempts": 3,
                "payload": {},
            },
        )

    assert unknown.status_code == 422
    assert invalid.status_code == 422
