from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.runtime.scheduler import SchedulerService
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本文件单元测试持久化 Cron 计划校验、启停和 APScheduler 同步服务。"""


# 调度测试统一使用的确定性 UTC 时间。
SCHEDULE_NOW = datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc)


def create_runtime_database(database_path: Path) -> None:
    """创建 Scheduler 单元测试独占的十表 SQLite 数据库。

    Args:
        database_path: 等待创建的临时应用数据库路径。
    """
    engine = create_application_engine(database_path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def build_schedule_payload(tmp_path: Path) -> dict:
    """构造不启用真实模型且所有路径均隔离的治理请求模板。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        可以被 SchedulerService 规范化并持久化的请求信封。
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


def test_scheduler_service_persists_syncs_and_toggles_schedule(tmp_path: Path) -> None:
    """计划应跨服务实例恢复、注册到 APScheduler，并可停用后移除进程内 Job。"""
    database_path = tmp_path / "application.sqlite3"
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    create_runtime_database(database_path)
    writer = SchedulerService(database_path, checkpoint_path)
    try:
        schedule = writer.create_schedule(
            name="每小时治理",
            cron_expression="0 * * * *",
            timezone_name="Asia/Shanghai",
            request_payload=build_schedule_payload(tmp_path),
            now=SCHEDULE_NOW,
        )
    finally:
        writer.close()

    service = SchedulerService(database_path, checkpoint_path)
    try:
        assert schedule["enabled"] is True
        assert schedule["next_run_at"] is not None
        assert service.list_schedules()[0]["id"] == schedule["id"]
        assert service.sync_schedules(now=SCHEDULE_NOW) == 1
        assert len(service.scheduler.get_jobs()) == 1

        disabled = service.set_schedule_enabled(
            schedule["id"],
            enabled=False,
            now=SCHEDULE_NOW,
        )
        assert disabled["enabled"] is False
        assert disabled["next_run_at"] is None
        assert service.sync_schedules(now=SCHEDULE_NOW) == 0
        assert service.scheduler.get_jobs() == []
    finally:
        service.close()


@pytest.mark.parametrize(
    ("cron_expression", "timezone_name"),
    [
        ("不是 Cron", "Asia/Shanghai"),
        ("0 * * * *", "Invalid/Timezone"),
    ],
)
def test_scheduler_service_rejects_invalid_cron_or_timezone(
    tmp_path: Path,
    cron_expression: str,
    timezone_name: str,
) -> None:
    """非法 Cron 或 IANA 时区必须在写入 scheduled_jobs 前被拒绝。"""
    database_path = tmp_path / "application.sqlite3"
    create_runtime_database(database_path)
    service = SchedulerService(database_path, tmp_path / "checkpoint.sqlite3")
    try:
        with pytest.raises(ValueError):
            service.create_schedule(
                name="非法计划",
                cron_expression=cron_expression,
                timezone_name=timezone_name,
                request_payload=build_schedule_payload(tmp_path),
            )
        assert service.list_schedules() == []
    finally:
        service.close()
