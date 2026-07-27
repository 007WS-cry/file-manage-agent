from __future__ import annotations

from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.runtime.job_queue import JobQueue
from app.runtime.worker import BackgroundWorker
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本文件集成测试两类 LangGraph 中断通过后台 API 暂停、幂等恢复和续跑。"""


def create_runtime_database(database_path: Path) -> None:
    """创建后台恢复 API 测试独占的应用数据库。

    Args:
        database_path: 等待创建的临时 SQLite 文件。
    """
    engine = create_application_engine(database_path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def create_docx(path: Path, text: str) -> None:
    """创建人工审核后台运行使用的最小 DOCX 文件。

    Args:
        path: DOCX 输出路径。
        text: 写入首个段落的正文。
    """
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def build_submission(
    tmp_path: Path,
    *,
    suffix: str,
    create_input: bool,
    auto_select_threshold: float = 0.82,
) -> dict:
    """构造可触发人工审核或请求校验恢复的后台提交。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。
        suffix: 用于隔离当前请求目录的后缀。
        create_input: 是否创建请求输入目录。
        auto_select_threshold: 自动确认主版本所需的最低置信度。

    Returns:
        满足运行提交 API 的完整 JSON 对象。
    """
    input_root = tmp_path / f"input-{suffix}"
    if create_input:
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
                "auto_select_threshold": auto_select_threshold,
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
        },
    }


def test_file_review_interrupt_resumes_idempotently_without_retry_cost(
    tmp_path: Path,
) -> None:
    """主版本人工审核应通过 API 恢复，重复请求不重复执行且不消耗异常次数。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    submission = build_submission(
        tmp_path,
        suffix="review",
        create_input=True,
        auto_select_threshold=1.0,
    )
    input_root = Path(submission["payload"]["workspace"]["input_root"])
    create_docx(input_root / "proposal_v1.docx", "Amount CNY 1000")
    create_docx(input_root / "proposal_v2.docx", "Amount CNY 1200")
    application = create_app(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
    )

    with TestClient(application) as client:
        receipt = client.post("/runs", json=submission).json()
        worker_queue = JobQueue(database_path)
        try:
            worker = BackgroundWorker(
                worker_queue,
                worker_id="worker-review-api",
                lease_seconds=30.0,
                heartbeat_interval_seconds=5.0,
            )
            assert worker.run_once() is True

            paused = client.get(f"/runs/{receipt['run_id']}").json()
            job = paused["background_job"]
            interrupt = job["pending_interrupt"]
            assert job["status"] == "waiting_human"
            assert job["attempt_count"] == 1
            assert interrupt["kind"] == "file_governance_review"

            stale = client.post(
                f"/runs/{receipt['run_id']}/resume",
                json={
                    "request_id": "review-stale",
                    "interrupt_id": "expired-interrupt",
                    "kind": "file_governance_review",
                    "value": {"selections": {}},
                },
            )
            assert stale.status_code == 409

            review_group = interrupt["payload"]["groups"][0]
            invalid_value = client.post(
                f"/runs/{receipt['run_id']}/resume",
                json={
                    "request_id": "review-invalid-value",
                    "interrupt_id": interrupt["interrupt_id"],
                    "kind": "file_governance_review",
                    "value": {"selections": {}},
                },
            )
            assert invalid_value.status_code == 422

            resume_body = {
                "request_id": "review-resume-001",
                "interrupt_id": interrupt["interrupt_id"],
                "kind": "file_governance_review",
                "value": {
                    "selections": {
                        review_group["group_id"]: review_group["candidates"][-1]["file_id"]
                    },
                    "review_note": "后台 API 人工确认",
                },
            }
            first = client.post(
                f"/runs/{receipt['run_id']}/resume",
                json=resume_body,
            )
            duplicate = client.post(
                f"/runs/{receipt['run_id']}/resume",
                json=resume_body,
            )

            assert first.status_code == 202
            assert duplicate.status_code == 202
            assert first.json() == duplicate.json()
            assert first.json()["status"] == "resume_queued"
            assert first.json()["attempt_count"] == 1

            assert worker.run_once() is True
            completed = client.get(f"/runs/{receipt['run_id']}").json()["background_job"]
            assert completed["status"] == "completed"
            assert completed["attempt_count"] == 1
            assert completed["resume_count"] == 1
            assert completed["pending_interrupt"] is None

            applied_duplicate = client.post(
                f"/runs/{receipt['run_id']}/resume",
                json=resume_body,
            )
            assert applied_duplicate.status_code == 202
            assert applied_duplicate.json()["status"] == "completed"
            assert applied_duplicate.json()["resume_count"] == 1
        finally:
            worker_queue.close()


def test_error_recovery_interrupt_resumes_through_api(tmp_path: Path) -> None:
    """请求校验错误应以独立恢复协议暂停，并可通过 API 安全终止后续图。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    application = create_app(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
    )

    with TestClient(application) as client:
        response = client.post(
            "/runs",
            json=build_submission(
                tmp_path,
                suffix="recovery",
                create_input=False,
            ),
        )
        assert response.status_code == 202
        receipt = response.json()
        worker_queue = JobQueue(database_path)
        try:
            worker = BackgroundWorker(
                worker_queue,
                worker_id="worker-recovery-api",
                lease_seconds=30.0,
                heartbeat_interval_seconds=5.0,
            )
            assert worker.run_once() is True
            paused = client.get(f"/runs/{receipt['run_id']}").json()["background_job"]
            interrupt = paused["pending_interrupt"]

            assert paused["status"] == "waiting_human"
            assert interrupt["kind"] == "error_recovery"
            assert "abort" in interrupt["payload"]["allowed_actions"]

            resume = client.post(
                f"/runs/{receipt['run_id']}/resume",
                json={
                    "request_id": "recovery-abort-001",
                    "interrupt_id": interrupt["interrupt_id"],
                    "kind": "error_recovery",
                    "value": {"action": "abort"},
                },
            )
            assert resume.status_code == 202
            assert resume.json()["attempt_count"] == 1

            assert worker.run_once() is True
            completed = client.get(f"/runs/{receipt['run_id']}").json()["background_job"]
            assert completed["status"] == "failed"
            assert completed["attempt_count"] == 1
            assert completed["resume_count"] == 1
            assert completed["pending_interrupt"] is None
        finally:
            worker_queue.close()
