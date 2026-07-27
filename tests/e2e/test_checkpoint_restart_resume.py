from __future__ import annotations

from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.runtime.job_queue import JobQueue
from app.runtime.worker import BackgroundWorker
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本文件端到端验收 API/Worker 重启后两类人工中断依靠持久化 checkpoint 恢复。"""


def create_runtime_database(database_path: Path) -> None:
    """创建 checkpoint 重启测试独占的应用数据库。

    Args:
        database_path: 等待初始化的临时 SQLite 文件。
    """
    engine = create_application_engine(database_path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def create_docx(path: Path, text: str) -> None:
    """创建触发版本审核的最小 DOCX。

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
    review_threshold: float = 0.82,
) -> dict:
    """构造主版本审核或错误恢复使用的后台提交。

    Args:
        tmp_path: pytest 当前测试的临时目录。
        suffix: 隔离当前场景路径的后缀。
        create_input: 是否创建请求输入目录。
        review_threshold: 自动选择主版本的置信度阈值。

    Returns:
        可由 ``POST /runs`` 接收的后台提交对象。
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
                "max_files": 20,
                "grouping_similarity_threshold": 0.72,
                "auto_select_threshold": review_threshold,
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
            "llm": {"enabled": False},
            "email_mcp": {"enabled": False},
        },
    }


def run_one_worker(database_path: Path, *, worker_id: str) -> None:
    """创建一个短生命周期 Worker 并执行队列中的单个任务。

    Args:
        database_path: API 与 Worker 共用的应用数据库。
        worker_id: 当前重启阶段使用的稳定 Worker ID。
    """
    queue = JobQueue(database_path)
    try:
        worker = BackgroundWorker(
            queue,
            worker_id=worker_id,
            lease_seconds=30.0,
            heartbeat_interval_seconds=5.0,
        )
        assert worker.run_once() is True
    finally:
        queue.close()


def test_review_checkpoint_survives_api_and_worker_restart(tmp_path: Path) -> None:
    """主版本审核暂停后应由新 API 与新 Worker 幂等恢复并下载报告。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    submission = build_submission(
        tmp_path,
        suffix="review",
        create_input=True,
        review_threshold=1.0,
    )
    input_root = Path(submission["payload"]["workspace"]["input_root"])
    create_docx(input_root / "proposal_v1.docx", "Amount CNY 1000")
    create_docx(input_root / "proposal_v2.docx", "Amount CNY 1200")

    first_application = create_app(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
    )
    with TestClient(first_application) as first_client:
        receipt = first_client.post("/runs", json=submission).json()
        run_one_worker(database_path, worker_id="e2e-review-worker-before-restart")
        paused = first_client.get(f"/runs/{receipt['run_id']}").json()
        interrupt = paused["background_job"]["pending_interrupt"]

    assert checkpoint_path.is_file()
    assert interrupt["kind"] == "file_governance_review"
    review_group = interrupt["payload"]["groups"][0]
    response_body = {
        "request_id": "e2e-review-resume-001",
        "interrupt_id": interrupt["interrupt_id"],
        "kind": "file_governance_review",
        "value": {
            "selections": {
                review_group["group_id"]: review_group["candidates"][-1]["file_id"]
            },
            "review_note": "API 和 Worker 重启后确认",
        },
    }

    restarted_application = create_app(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
    )
    with TestClient(restarted_application) as restarted_client:
        stale = restarted_client.post(
            f"/runs/{receipt['run_id']}/resume",
            json={**response_body, "interrupt_id": "expired-interrupt"},
        )
        first_resume = restarted_client.post(
            f"/runs/{receipt['run_id']}/resume",
            json=response_body,
        )
        duplicate_resume = restarted_client.post(
            f"/runs/{receipt['run_id']}/resume",
            json=response_body,
        )

        assert stale.status_code == 409
        assert first_resume.status_code == 202
        assert first_resume.json() == duplicate_resume.json()
        assert first_resume.json()["attempt_count"] == 1
        run_one_worker(database_path, worker_id="e2e-review-worker-after-restart")
        completed = restarted_client.get(f"/runs/{receipt['run_id']}").json()
        report = restarted_client.get(f"/runs/{receipt['run_id']}/report")

    completed_job = completed["background_job"]
    assert completed_job["status"] == "completed"
    assert completed_job["attempt_count"] == 1
    assert completed_job["resume_count"] == 1
    assert report.status_code == 200


def test_recovery_checkpoint_survives_worker_restart_without_retry_cost(
    tmp_path: Path,
) -> None:
    """错误恢复型人工终止应由新 Worker 应用且不增加异常尝试次数。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    submission = build_submission(
        tmp_path,
        suffix="recovery",
        create_input=False,
    )
    application = create_app(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
    )

    with TestClient(application) as client:
        receipt = client.post("/runs", json=submission).json()
        run_one_worker(database_path, worker_id="e2e-recovery-worker-before-restart")
        paused = client.get(f"/runs/{receipt['run_id']}").json()["background_job"]
        interrupt = paused["pending_interrupt"]
        assert paused["attempt_count"] == 1
        assert interrupt["kind"] == "error_recovery"

    restarted_application = create_app(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
    )
    with TestClient(restarted_application) as restarted_client:
        resume = restarted_client.post(
            f"/runs/{receipt['run_id']}/resume",
            json={
                "request_id": "e2e-recovery-abort-001",
                "interrupt_id": interrupt["interrupt_id"],
                "kind": "error_recovery",
                "value": {"action": "abort"},
            },
        )
        assert resume.status_code == 202
        assert resume.json()["attempt_count"] == 1
        run_one_worker(database_path, worker_id="e2e-recovery-worker-after-restart")
        completed = restarted_client.get(
            f"/runs/{receipt['run_id']}"
        ).json()["background_job"]

    assert completed["status"] == "failed"
    assert completed["attempt_count"] == 1
    assert completed["resume_count"] == 1
