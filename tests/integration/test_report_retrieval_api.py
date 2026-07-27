from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.runtime.job_queue import JobQueue
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本文件集成测试后台治理报告下载及 report_root 路径越界防护。"""


def create_runtime_database(database_path: Path) -> None:
    """创建报告下载 API 测试独占的应用数据库。

    Args:
        database_path: 等待创建的临时 SQLite 文件。
    """
    engine = create_application_engine(database_path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def build_submission(tmp_path: Path, *, suffix: str) -> dict:
    """构造报告下载测试使用的后台提交请求。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。
        suffix: 用于隔离输入、产物和报告目录的后缀。

    Returns:
        满足运行提交 API 的完整 JSON 对象。
    """
    input_root = tmp_path / f"input-{suffix}"
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
                "artifact_root": str(tmp_path / f"artifacts-{suffix}"),
                "report_root": str(tmp_path / f"reports-{suffix}"),
            },
        },
    }


def finish_job_with_report(
    queue: JobQueue,
    *,
    job_id: str,
    report_path: Path,
    worker_id: str,
) -> None:
    """领取并以指定报告路径收口一个测试后台任务。

    Args:
        queue: 当前测试共享的后台任务队列。
        job_id: 等待收口的任务 ID。
        report_path: 写入任务记录的报告路径。
        worker_id: 当前测试使用的租约 Worker ID。
    """
    claimed = queue.claim(worker_id, lease_seconds=30.0)
    assert claimed is not None
    assert claimed["id"] == job_id
    queue.mark_running(job_id, worker_id=worker_id)
    queue.finish(
        job_id,
        worker_id=worker_id,
        status="completed",
        report_path=str(report_path),
    )


def test_report_can_download_and_exposes_controlled_url(tmp_path: Path) -> None:
    """已登记且位于 report_root 内的 Markdown 报告应可按 run_id 下载。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    application = create_app(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
    )

    with TestClient(application) as client:
        submission = build_submission(tmp_path, suffix="safe")
        receipt = client.post("/runs", json=submission).json()
        report_root = Path(submission["payload"]["workspace"]["report_root"])
        report_root.mkdir(parents=True)
        report_path = report_root / "governance.md"
        report_text = "# 安全报告\n\n下载成功。"
        report_path.write_text(report_text, encoding="utf-8")
        queue = JobQueue(database_path)
        try:
            finish_job_with_report(
                queue,
                job_id=receipt["job_id"],
                report_path=report_path,
                worker_id="worker-report-safe",
            )
        finally:
            queue.close()

        status_response = client.get(f"/runs/{receipt['run_id']}")
        report_response = client.get(f"/runs/{receipt['run_id']}/report")

    assert status_response.status_code == 200
    assert status_response.json()["report_available"] is True
    assert status_response.json()["report_url"] == f"/runs/{receipt['run_id']}/report"
    assert report_response.status_code == 200
    assert report_response.content == report_path.read_bytes()
    assert report_response.headers["content-type"].startswith("text/markdown")
    assert f"{receipt['run_id']}.md" in report_response.headers["content-disposition"]


def test_report_download_rejects_path_outside_report_root(tmp_path: Path) -> None:
    """数据库中的报告路径即使存在，也不得越过当前任务声明的 report_root。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    application = create_app(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
    )

    with TestClient(application) as client:
        submission = build_submission(tmp_path, suffix="escape")
        receipt = client.post("/runs", json=submission).json()
        report_root = Path(submission["payload"]["workspace"]["report_root"])
        report_root.mkdir(parents=True)
        outside_path = tmp_path / "outside-report.md"
        outside_path.write_text("# 不应下载", encoding="utf-8")
        queue = JobQueue(database_path)
        try:
            finish_job_with_report(
                queue,
                job_id=receipt["job_id"],
                report_path=outside_path,
                worker_id="worker-report-escape",
            )
        finally:
            queue.close()

        response = client.get(f"/runs/{receipt['run_id']}/report")

    assert response.status_code == 409
    assert "report_root" in response.json()["detail"]
