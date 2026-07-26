from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.runtime.scheduler import SchedulerService
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本文件集成验证 Cron 回调只创建后台任务并登记最近运行，不执行 LangGraph。"""


def create_runtime_database(database_path: Path) -> None:
    """创建 Cron 入队集成测试独占的十表 SQLite 数据库。

    Args:
        database_path: 等待创建的临时应用数据库路径。
    """
    engine = create_application_engine(database_path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def build_schedule_payload(tmp_path: Path) -> dict:
    """构造 Cron 入队测试使用的最小只读治理请求。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        可持久化到 scheduled_jobs 并复制到 background_jobs 的请求模板。
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


def test_cron_callback_only_enqueues_background_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Cron 触发应返回 queued 任务，且不能调用治理图构建或 invoke。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    service = SchedulerService(database_path, checkpoint_path)

    def reject_graph_execution(*args, **kwargs):
        """在 Scheduler 错误执行 LangGraph 时立即使测试失败。"""
        raise AssertionError("Scheduler 不得构建或执行 LangGraph")

    monkeypatch.setattr(
        "app.runtime.dispatcher.build_file_governance_graph",
        reject_graph_execution,
    )
    try:
        schedule = service.create_schedule(
            name="每分钟治理",
            cron_expression="* * * * *",
            timezone_name="UTC",
            request_payload=build_schedule_payload(tmp_path),
        )
        job = service.enqueue_schedule(
            schedule["id"],
            triggered_at=datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc),
        )

        assert job is not None
        assert job["status"] == "queued"
        assert job["trigger_source"] == "cron"
        assert job["attempt_count"] == 0
        stored_schedule = service.get_schedule(schedule["id"])
        assert stored_schedule is not None
        assert stored_schedule["last_run_id"] == job["run_id"]
        assert stored_schedule["last_triggered_at"] is not None
    finally:
        service.close()
