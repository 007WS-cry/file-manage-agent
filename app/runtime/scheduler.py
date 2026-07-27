from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.runtime.dispatcher import (
    create_background_submission,
    normalize_runtime_envelope,
)
from app.runtime.job_queue import JobQueue, utc_now
from app.state.models import BackgroundJobState, ScheduledJobState
from app.storage.database import (
    ApplicationDatabaseTarget,
    create_application_engine,
    create_session_factory,
    open_application_session,
    render_safe_application_database_target,
)
from app.storage.orm_models import ScheduledJobModel
from app.storage.repositories import create_repository_bundle

"""本模块使用 APScheduler 恢复持久化 Cron 计划，触发时只向后台队列写入任务。"""


# APScheduler 内部注册持久化计划时使用的固定 Job ID 前缀。
SCHEDULE_JOB_ID_PREFIX = "file-governance-schedule:"

# APScheduler 定期从应用数据库同步计划时使用的固定内部 Job ID。
SCHEDULE_RECONCILE_JOB_ID = "file-governance-schedule-reconcile"

# 独立 Scheduler 默认解释自身维护任务所使用的时区。
DEFAULT_SCHEDULER_TIMEZONE = "Asia/Shanghai"

# Scheduler 默认扫描 API 新增或修改计划的间隔秒数。
DEFAULT_SCHEDULE_RECONCILE_INTERVAL_SECONDS = 15.0

# Cron 触发的后台任务默认允许的 Worker 总尝试次数。
DEFAULT_SCHEDULE_MAX_ATTEMPTS = 3


def _ensure_aware_utc(value: datetime) -> datetime:
    """把 datetime 规范化为带 UTC 时区的值。

    Args:
        value: APScheduler、SQLAlchemy 或测试提供的时间。

    Returns:
        时区为 UTC 的 datetime。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_to_iso(value: datetime | None) -> str | None:
    """把可选数据库时间转换为 UTC ISO 8601 字符串。

    Args:
        value: SQLAlchemy 返回的 datetime 或 None。

    Returns:
        UTC ISO 8601 字符串；输入为空时返回 None。
    """
    return _ensure_aware_utc(value).isoformat() if value is not None else None


def create_cron_trigger(cron_expression: str, timezone_name: str) -> CronTrigger:
    """校验五段 Cron 表达式和 IANA 时区并创建 APScheduler Trigger。

    Args:
        cron_expression: 标准五段 crontab 表达式。
        timezone_name: 用于解释 Cron 的 IANA 时区名称。

    Returns:
        可以注册到 APScheduler 的 CronTrigger。

    Raises:
        ValueError: Cron 表达式或时区不合法时抛出。
    """
    expression = cron_expression.strip() if isinstance(cron_expression, str) else ""
    normalized_timezone = timezone_name.strip() if isinstance(timezone_name, str) else ""
    if not expression or len(expression) > 160:
        raise ValueError("cron_expression 必须是长度不超过 160 的非空字符串")
    if not normalized_timezone or len(normalized_timezone) > 64:
        raise ValueError("timezone 必须是长度不超过 64 的非空 IANA 时区")
    try:
        zone = ZoneInfo(normalized_timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"未知 IANA 时区：{normalized_timezone}") from error
    try:
        return CronTrigger.from_crontab(expression, timezone=zone)
    except ValueError as error:
        raise ValueError(f"cron_expression 不是合法五段 Cron：{error}") from error


def calculate_next_run_at(
    trigger: CronTrigger,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """计算 Cron Trigger 在指定时间之后的下一次触发时间。

    Args:
        trigger: 已通过调度层校验的 APScheduler Cron Trigger。
        now: 可选当前时间；省略时使用当前 UTC 时间。

    Returns:
        下一次触发的 UTC 时间；规则不再触发时返回 None。
    """
    current = _ensure_aware_utc(now or utc_now())
    next_run = trigger.get_next_fire_time(None, current)
    return _ensure_aware_utc(next_run) if next_run is not None else None


def scheduled_job_to_state(schedule: ScheduledJobModel) -> ScheduledJobState:
    """把定时计划 ORM 对象复制为不持有 Session 的状态。

    Args:
        schedule: 当前事务内读取的 ScheduledJobModel。

    Returns:
        可安全用于 API、Scheduler 同步和测试的 ScheduledJobState。
    """
    return ScheduledJobState(
        id=schedule.schedule_id,
        name=schedule.name,
        cron_expression=schedule.cron_expression,
        timezone=schedule.timezone,
        enabled=schedule.enabled,
        request_payload=dict(schedule.request_payload),
        last_triggered_at=_datetime_to_iso(schedule.last_triggered_at),
        last_run_id=schedule.last_run_id,
        next_run_at=_datetime_to_iso(schedule.next_run_at),
        last_error=schedule.last_error,
        created_at=_datetime_to_iso(schedule.created_at) or "",
        updated_at=_datetime_to_iso(schedule.updated_at) or "",
    )


class SchedulerService:
    """以 scheduled_jobs 为事实来源注册 Cron，并只创建持久化后台任务。"""

    def __init__(
        self,
        database_path: ApplicationDatabaseTarget,
        checkpoint_path: str | Path,
        *,
        timezone_name: str = DEFAULT_SCHEDULER_TIMEZONE,
        reconcile_interval_seconds: float = DEFAULT_SCHEDULE_RECONCILE_INTERVAL_SECONDS,
        max_attempts: int = DEFAULT_SCHEDULE_MAX_ATTEMPTS,
        queue: JobQueue | None = None,
    ) -> None:
        """创建可供 API 管理计划或独立进程运行的 Scheduler 服务。

        本构造函数不会启动 APScheduler，也不会执行 LangGraph。API 可以使用同一
        服务完成计划 CRUD；只有独立入口调用 ``run_forever`` 后才会按 Cron 回调，
        回调也只调用持久化入队函数。

        Args:
            database_path: 已执行最新迁移的 SQLite 路径或 PostgreSQL URL。
            checkpoint_path: Cron 创建的后台任务与 Worker 共用的 checkpoint 路径。
            timezone_name: Scheduler 内部同步任务使用的 IANA 时区。
            reconcile_interval_seconds: 从数据库同步新增、启停计划的间隔秒数。
            max_attempts: Cron 后台任务允许的 Worker 总尝试次数。
            queue: 可选共享 JobQueue；API lifespan 使用它避免重复持有队列。

        Raises:
            ValueError: 时区、同步间隔或最大尝试次数不合法时抛出。
        """
        try:
            scheduler_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"未知 Scheduler IANA 时区：{timezone_name}") from error
        if (
            isinstance(reconcile_interval_seconds, bool)
            or not isinstance(reconcile_interval_seconds, (int, float))
            or reconcile_interval_seconds < 1
            or reconcile_interval_seconds > 3_600
        ):
            raise ValueError("reconcile_interval_seconds 必须位于 1 到 3600 秒之间")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts 必须是整数")
        if max_attempts < 1 or max_attempts > 20:
            raise ValueError("max_attempts 必须位于 1 到 20 之间")

        self.database_target = database_path
        # 当前进程内用于创建连接的数据库目标；不得写入持久化请求或日志。

        self.database_path = render_safe_application_database_target(database_path)
        # 兼容诊断属性；PostgreSQL 值会隐藏密码。

        self.checkpoint_path = str(Path(checkpoint_path).expanduser().resolve())
        # Cron 创建的后台任务交给 Worker 使用的 checkpoint 绝对路径。

        self.reconcile_interval_seconds = float(reconcile_interval_seconds)
        # Scheduler 重新读取计划表的固定间隔秒数。

        self.max_attempts = max_attempts
        # Cron 创建的每个后台任务允许的 Worker 总尝试次数。

        self._engine: Engine = create_application_engine(self.database_target)
        # 计划 CRUD 和最近触发状态使用的独立 SQLAlchemy Engine。

        self._session_factory: sessionmaker = create_session_factory(self._engine)
        # 每次计划操作创建短事务 Session 的工厂。

        self._owns_queue = queue is None
        # 服务关闭时是否需要同时关闭内部创建的 JobQueue。

        self._queue = queue or JobQueue(self.database_target)
        # Cron 回调唯一允许写入的持久化后台队列。

        self._scheduler = BlockingScheduler(timezone=scheduler_timezone)
        # 只保存可从 scheduled_jobs 重建的进程内 APScheduler 实例。

    @property
    def scheduler(self) -> BlockingScheduler:
        """返回当前进程内 Scheduler，供入口状态检查和白盒测试使用。

        Returns:
            尚未启动或正在运行的 BlockingScheduler。
        """
        return self._scheduler

    def create_schedule(
        self,
        *,
        name: str,
        cron_expression: str,
        timezone_name: str,
        request_payload: dict[str, Any],
        enabled: bool = True,
        schedule_id: str | None = None,
        now: datetime | None = None,
    ) -> ScheduledJobState:
        """校验并持久化一个可由独立 Scheduler 恢复的 Cron 计划。

        本方法不会启动 APScheduler或执行 LangGraph。请求模板会先经过与后台提交
        相同的路径和 JSON 校验，以确保将来触发时能够直接创建后台任务。

        Args:
            name: 面向用户展示的计划名称。
            cron_expression: 标准五段 Cron 表达式。
            timezone_name: 解释 Cron 的 IANA 时区。
            request_payload: 包含 request 与 workspace 的治理请求模板。
            enabled: 创建后是否允许独立 Scheduler 注册。
            schedule_id: 可选稳定 ID；省略时自动生成。
            now: 可选创建时间，便于确定性测试。

        Returns:
            已提交数据库的完整计划状态。
        """
        normalized_name = name.strip() if isinstance(name, str) else ""
        if not normalized_name or len(normalized_name) > 160:
            raise ValueError("name 必须是长度不超过 160 的非空字符串")
        if not isinstance(enabled, bool):
            raise TypeError("enabled 必须是布尔值")
        trigger = create_cron_trigger(cron_expression, timezone_name)
        normalized_payload = normalize_runtime_envelope(
            request_payload,
            application_database_path=self.database_target,
            checkpoint_path=self.checkpoint_path,
        )
        created_at = _ensure_aware_utc(now or utc_now())
        next_run_at = (
            calculate_next_run_at(trigger, now=created_at) if enabled else None
        )
        normalized_schedule_id = schedule_id.strip() if isinstance(schedule_id, str) else ""
        state = ScheduledJobState(
            id=normalized_schedule_id or uuid4().hex,
            name=normalized_name,
            cron_expression=cron_expression.strip(),
            timezone=timezone_name.strip(),
            enabled=enabled,
            request_payload=normalized_payload,
            last_triggered_at=None,
            last_run_id=None,
            next_run_at=_datetime_to_iso(next_run_at),
            last_error=None,
            created_at=created_at.isoformat(),
            updated_at=created_at.isoformat(),
        )
        with open_application_session(self._session_factory) as session:
            record = create_repository_bundle(session).scheduled_jobs.add_state(state)
            return scheduled_job_to_state(record)

    def get_schedule(self, schedule_id: str) -> ScheduledJobState | None:
        """按照 ID 查询一项持久化计划。

        Args:
            schedule_id: 创建接口返回的计划 ID。

        Returns:
            找到时返回独立状态，否则返回 None。
        """
        with open_application_session(self._session_factory) as session:
            record = create_repository_bundle(session).scheduled_jobs.get(schedule_id)
            return scheduled_job_to_state(record) if record is not None else None

    def list_schedules(self, *, limit: int = 500) -> list[ScheduledJobState]:
        """列出启用和停用的持久化计划。

        Args:
            limit: 单次允许返回的最大计划数量。

        Returns:
            按创建时间稳定排序的计划状态列表。
        """
        with open_application_session(self._session_factory) as session:
            records = create_repository_bundle(session).scheduled_jobs.list_all(limit=limit)
            return [scheduled_job_to_state(record) for record in records]

    def set_schedule_enabled(
        self,
        schedule_id: str,
        *,
        enabled: bool,
        now: datetime | None = None,
    ) -> ScheduledJobState:
        """启用或停用一项计划，并更新下一次预计触发时间。

        Args:
            schedule_id: 等待修改的计划 ID。
            enabled: 修改后的启用状态。
            now: 可选状态变化时间。

        Returns:
            已提交数据库的计划状态。
        """
        if not isinstance(enabled, bool):
            raise TypeError("enabled 必须是布尔值")
        changed_at = _ensure_aware_utc(now or utc_now())
        with open_application_session(self._session_factory) as session:
            repositories = create_repository_bundle(session)
            schedule = repositories.scheduled_jobs.get_required(schedule_id)
            trigger = create_cron_trigger(
                schedule.cron_expression,
                schedule.timezone,
            )
            next_run_at = (
                calculate_next_run_at(trigger, now=changed_at) if enabled else None
            )
            record = repositories.scheduled_jobs.set_enabled(
                schedule_id,
                enabled=enabled,
                next_run_at=next_run_at,
                updated_at=changed_at,
            )
            return scheduled_job_to_state(record)

    def sync_schedules(self, *, now: datetime | None = None) -> int:
        """把数据库中的启用计划同步到当前进程内 APScheduler。

        API 新增或启停计划后，独立 Scheduler 最迟在下一次同步周期看到变化。
        数据库仍是唯一事实来源；进程内 Job 不使用第二套持久化 JobStore。

        Args:
            now: 可选同步时间，便于确定性测试。

        Returns:
            本次成功注册或更新的启用计划数量。
        """
        synchronized_at = _ensure_aware_utc(now or utc_now())
        schedules = self.list_schedules()
        expected_job_ids = {
            f"{SCHEDULE_JOB_ID_PREFIX}{schedule['id']}"
            for schedule in schedules
            if schedule["enabled"]
        }
        for job in self._scheduler.get_jobs():
            if (
                job.id.startswith(SCHEDULE_JOB_ID_PREFIX)
                and job.id not in expected_job_ids
            ):
                self._scheduler.remove_job(job.id)

        synchronized = 0
        for schedule in schedules:
            if not schedule["enabled"]:
                continue
            try:
                trigger = create_cron_trigger(
                    schedule["cron_expression"],
                    schedule["timezone"],
                )
                self._scheduler.add_job(
                    self.enqueue_schedule,
                    trigger=trigger,
                    args=(schedule["id"],),
                    id=f"{SCHEDULE_JOB_ID_PREFIX}{schedule['id']}",
                    name=schedule["name"],
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=60,
                )
                next_run_at = calculate_next_run_at(trigger, now=synchronized_at)
                with open_application_session(self._session_factory) as session:
                    create_repository_bundle(session).scheduled_jobs.update_registration(
                        schedule["id"],
                        next_run_at=next_run_at,
                        error_summary=None,
                        updated_at=synchronized_at,
                    )
                synchronized += 1
            except (TypeError, ValueError, RuntimeError) as error:
                with open_application_session(self._session_factory) as session:
                    create_repository_bundle(session).scheduled_jobs.record_error(
                        schedule["id"],
                        error_summary=(
                            f"{type(error).__name__}: Scheduler 注册计划失败。"
                        ),
                        updated_at=synchronized_at,
                    )
        return synchronized

    def enqueue_schedule(
        self,
        schedule_id: str,
        *,
        triggered_at: datetime | None = None,
    ) -> BackgroundJobState | None:
        """由 Cron 回调创建一个后台任务，不调用或运行 LangGraph。

        Args:
            schedule_id: APScheduler Job 绑定的持久化计划 ID。
            triggered_at: 可选实际触发时间，便于确定性集成测试。

        Returns:
            成功时返回状态为 queued 且 trigger_source 为 cron 的后台任务；
            计划已经停用时返回 None。

        Raises:
            RuntimeError: 后台任务入队或计划状态登记失败时抛出。
        """
        fired_at = _ensure_aware_utc(triggered_at or utc_now())
        schedule = self.get_schedule(schedule_id)
        if schedule is None:
            raise RuntimeError("Cron 触发引用了不存在的持久化计划")
        if not schedule["enabled"]:
            return None
        try:
            job = create_background_submission(
                schedule["request_payload"],
                queue=self._queue,
                checkpoint_path=self.checkpoint_path,
                max_attempts=self.max_attempts,
                trigger_source="cron",
            )
            trigger = create_cron_trigger(
                schedule["cron_expression"],
                schedule["timezone"],
            )
            next_run_at = calculate_next_run_at(trigger, now=fired_at)
            with open_application_session(self._session_factory) as session:
                create_repository_bundle(session).scheduled_jobs.record_trigger(
                    schedule_id,
                    triggered_at=fired_at,
                    run_id=job["run_id"],
                    next_run_at=next_run_at,
                )
            return job
        except Exception as error:
            with open_application_session(self._session_factory) as session:
                create_repository_bundle(session).scheduled_jobs.record_error(
                    schedule_id,
                    error_summary=f"{type(error).__name__}: Cron 后台任务入队失败。",
                    updated_at=fired_at,
                )
            raise

    def run_forever(self) -> None:
        """同步持久化计划并阻塞运行 APScheduler，直到进程关闭。"""
        self.sync_schedules()
        self._scheduler.add_job(
            self.sync_schedules,
            trigger="interval",
            seconds=self.reconcile_interval_seconds,
            id=SCHEDULE_RECONCILE_JOB_ID,
            name="同步持久化 Cron 计划",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.start()

    def stop(self, *, wait: bool = True) -> None:
        """请求正在运行的 APScheduler 安全停止。

        Args:
            wait: 是否等待正在执行的短入队回调完成。
        """
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)

    def close(self) -> None:
        """停止 Scheduler 并释放计划数据库和可选内部队列连接池。"""
        self.stop(wait=False)
        self._engine.dispose()
        if self._owns_queue:
            self._queue.close()
