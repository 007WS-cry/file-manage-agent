from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.state.models import (
    BackgroundJobState,
    BackgroundResumeState,
    PendingInterruptState,
    WorkerLeaseState,
)
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.orm_models import BackgroundJobModel, GovernanceRunModel, WorkerLeaseModel
from app.storage.repositories import create_repository_bundle
from app.utils.background_resume import build_background_resume_state

"""本模块在短数据库事务中编排后台任务入队、领取、心跳、完成和租约恢复。"""


# 后台任务已经产生最终结果、不得再被 Worker 重新打开的状态。
TERMINAL_JOB_STATUSES = frozenset({"completed", "partial", "failed"})

# Worker 可以将一次图执行收口到的状态。
WORKER_RESULT_STATUSES = TERMINAL_JOB_STATUSES | {"waiting_human"}


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。

    Returns:
        可用于队列可见时间、租约和持久化时间字段的 UTC datetime。
    """
    return datetime.now(timezone.utc)


def _datetime_to_iso(value: datetime | None) -> str | None:
    """把数据库 datetime 转换为 API 和状态使用的 ISO 8601 字符串。

    Args:
        value: SQLAlchemy 返回的可选 datetime。

    Returns:
        ISO 8601 字符串；输入为 None 时返回 None。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def background_job_to_state(job: BackgroundJobModel) -> BackgroundJobState:
    """把后台任务 ORM 对象复制为不持有 Session 的状态字典。

    Args:
        job: 当前事务内读取到的后台任务 ORM 对象。

    Returns:
        可以安全跨线程、跨进程序列化的 BackgroundJobState。
    """
    return BackgroundJobState(
        id=job.job_id,
        run_id=job.run_id,
        thread_id=job.thread_id,
        trigger_source=job.trigger_source,
        status=job.status,
        request_payload=dict(job.request_payload),
        current_worker_id=job.current_worker_id,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        resume_count=job.resume_count,
        pending_interrupt=(
            dict(job.pending_interrupt) if job.pending_interrupt is not None else None
        ),
        resume=dict(job.resume_state) if job.resume_state is not None else None,
        available_at=_datetime_to_iso(job.available_at) or "",
        claimed_at=_datetime_to_iso(job.claimed_at),
        started_at=_datetime_to_iso(job.started_at),
        report_path=job.report_path,
        error_summary=job.error_summary,
        created_at=_datetime_to_iso(job.created_at) or "",
        updated_at=_datetime_to_iso(job.updated_at) or "",
        finished_at=_datetime_to_iso(job.finished_at),
    )


def worker_lease_to_state(lease: WorkerLeaseModel) -> WorkerLeaseState:
    """把 Worker 租约 ORM 对象复制为不持有 Session 的状态字典。

    Args:
        lease: 当前事务内读取到的 Worker 租约 ORM 对象。

    Returns:
        可以安全用于心跳、状态查询和测试的 WorkerLeaseState。
    """
    return WorkerLeaseState(
        id=lease.lease_id,
        job_id=lease.job_id,
        worker_id=lease.worker_id,
        status=lease.status,
        acquired_at=_datetime_to_iso(lease.acquired_at) or "",
        heartbeat_at=_datetime_to_iso(lease.heartbeat_at) or "",
        expires_at=_datetime_to_iso(lease.expires_at) or "",
        released_at=_datetime_to_iso(lease.released_at),
        updated_at=_datetime_to_iso(lease.updated_at) or "",
    )


def governance_run_to_dict(run: GovernanceRunModel) -> dict[str, object]:
    """把治理运行 ORM 摘要转换为 API 可返回的脱敏字典。

    Args:
        run: 当前事务内读取到的治理运行 ORM 对象。

    Returns:
        不包含请求正文、文档内容或凭据的治理运行摘要。
    """
    return {
        "run_id": run.run_id,
        "thread_id": run.thread_id,
        "status": run.status,
        "current_stage": run.current_stage,
        "report_path": run.report_path,
        "error_summary": run.error_summary,
        "created_at": _datetime_to_iso(run.created_at),
        "started_at": _datetime_to_iso(run.started_at),
        "finished_at": _datetime_to_iso(run.finished_at),
        "updated_at": _datetime_to_iso(run.updated_at),
    }


class JobQueue:
    """使用独立 SQLAlchemy Engine 管理持久化后台任务和 Worker 租约。"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        timeout_seconds: float = 30.0,
        echo: bool = False,
    ) -> None:
        """创建可由 API、Worker 和状态查询共享数据库文件的队列服务。

        本类不会调用 ``Base.metadata.create_all`` 或自动执行 Alembic；调用方必须
        在启动 API/Worker 前执行 ``alembic upgrade head``。

        Args:
            database_path: 十张应用表共用的 SQLite 数据库路径。
            timeout_seconds: SQLite 等待短暂写锁释放的最大秒数。
            echo: 是否输出 SQLAlchemy SQL 日志。
        """
        self._engine: Engine = create_application_engine(
            database_path,
            timeout_seconds=timeout_seconds,
            echo=echo,
        )
        # 当前队列服务独占、但可以创建多个短 Session 的 SQLAlchemy Engine。

        self._session_factory: sessionmaker = create_session_factory(self._engine)
        # 绑定当前 Engine 且禁止跨线程共享 Session 的工厂。

        self.database_path = str(Path(database_path).expanduser().resolve())
        # 当前队列服务使用的应用数据库绝对路径。

    def close(self) -> None:
        """释放当前队列服务持有的 SQLAlchemy 连接池。"""
        self._engine.dispose()

    def enqueue(
        self,
        job: BackgroundJobState,
        *,
        request_summary: dict[str, object] | None = None,
    ) -> BackgroundJobState:
        """在一个事务中创建 queued 治理运行和后台任务。

        Args:
            job: 已规范化且尚未持久化的后台任务状态。
            request_summary: 可选脱敏请求计数和功能开关。

        Returns:
            数据库提交后重新读取的后台任务状态。
        """
        with open_application_session(self._session_factory) as session:
            repositories = create_repository_bundle(session)
            repositories.governance_runs.get_or_create_minimal(
                job["run_id"],
                thread_id=job["thread_id"],
                current_stage="background_queued",
                status="queued",
                request_summary=request_summary,
            )
            record = repositories.background_jobs.enqueue(job)
            return background_job_to_state(record)

    def get_job(self, job_id: str) -> BackgroundJobState | None:
        """按照后台任务 ID 查询当前队列状态。

        Args:
            job_id: 等待查询的后台任务 ID。

        Returns:
            找到时返回脱离 Session 的后台任务状态，否则返回 None。
        """
        with open_application_session(self._session_factory) as session:
            record = create_repository_bundle(session).background_jobs.get(job_id)
            return background_job_to_state(record) if record is not None else None

    def get_job_by_run_id(self, run_id: str) -> BackgroundJobState | None:
        """按照治理运行 ID 查询对应的后台任务。

        Args:
            run_id: 等待查询的治理运行 ID。

        Returns:
            找到时返回脱离 Session 的后台任务状态，否则返回 None。
        """
        with open_application_session(self._session_factory) as session:
            record = create_repository_bundle(session).background_jobs.find_by_run_id(run_id)
            return background_job_to_state(record) if record is not None else None

    def get_run(self, run_id: str) -> dict[str, object] | None:
        """按照治理运行 ID 查询脱敏运行摘要。

        Args:
            run_id: 等待查询的治理运行 ID。

        Returns:
            找到时返回 API 安全摘要，否则返回 None。
        """
        with open_application_session(self._session_factory) as session:
            record = create_repository_bundle(session).governance_runs.get(run_id)
            return governance_run_to_dict(record) if record is not None else None

    def enqueue_resume(
        self,
        run_id: str,
        *,
        request_id: str,
        interrupt_id: str,
        kind: str,
        value: dict,
        now: datetime | None = None,
    ) -> BackgroundJobState:
        """校验当前中断并幂等提交一次后台人工恢复请求。

        Args:
            run_id: 等待恢复的治理运行 ID。
            request_id: 调用方为本次恢复提供的幂等键。
            interrupt_id: 必须与当前 waiting_human 快照一致的中断 ID。
            kind: 人工审核或错误恢复协议类型。
            value: 交给 ``Command(resume=...)`` 的 JSON 对象。
            now: 测试可注入的当前 UTC 时间。

        Returns:
            已进入 ``resume_queued`` 或已由相同请求推进后的后台任务状态。

        Raises:
            LookupError: run_id 不存在后台任务时抛出。
            RuntimeError: 中断过期、状态冲突或幂等键冲突时抛出。
            ValueError: 恢复请求不符合 JSON、大小或协议类型约束时抛出。
        """
        submitted_at = now or utc_now()
        resume_state: BackgroundResumeState = build_background_resume_state(
            request_id=request_id,
            interrupt_id=interrupt_id,
            kind=kind,
            value=value,
            submitted_at=submitted_at.isoformat(),
        )
        with open_application_session(self._session_factory) as session:
            repositories = create_repository_bundle(session)
            existing = repositories.background_jobs.find_by_run_id(run_id)
            if existing is None:
                raise LookupError(f"后台运行不存在: {run_id}")
            record = repositories.background_jobs.queue_resume(
                existing.job_id,
                resume_state=dict(resume_state),
                queued_at=submitted_at,
            )
            if record.status == "resume_queued":
                repositories.governance_runs.update_status(
                    record.run_id,
                    status="queued",
                    current_stage="background_resume_queued",
                )
            return background_job_to_state(record)

    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> BackgroundJobState | None:
        """在一个事务中领取任务并建立同一任务的有效 Worker 租约。

        Args:
            worker_id: 尝试领取任务的 Worker ID。
            lease_seconds: 未收到心跳前租约保持有效的秒数。
            now: 测试可注入的当前 UTC 时间。

        Returns:
            成功领取后的后台任务；当前无可执行任务时返回 None。

        Raises:
            ValueError: 租约时长不大于零时抛出。
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于零")
        claimed_at = now or utc_now()
        expires_at = claimed_at + timedelta(seconds=lease_seconds)
        with open_application_session(self._session_factory) as session:
            repositories = create_repository_bundle(session)
            job = repositories.background_jobs.claim_next(
                worker_id=worker_id,
                claimed_at=claimed_at,
            )
            if job is None:
                return None
            lease = WorkerLeaseState(
                id=uuid4().hex,
                job_id=job.job_id,
                worker_id=worker_id,
                status="active",
                acquired_at=claimed_at.isoformat(),
                heartbeat_at=claimed_at.isoformat(),
                expires_at=expires_at.isoformat(),
                released_at=None,
                updated_at=claimed_at.isoformat(),
            )
            repositories.worker_leases.activate(lease)
            repositories.governance_runs.update_status(
                job.run_id,
                status="queued",
                current_stage=(
                    "background_resume_leased"
                    if isinstance(job.resume_state, dict)
                    and job.resume_state.get("status") == "pending"
                    else "background_leased"
                ),
            )
            return background_job_to_state(job)

    def mark_running(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> BackgroundJobState:
        """把已经建立租约的后台任务推进为 running。

        Args:
            job_id: 等待开始执行的后台任务 ID。
            worker_id: 当前持有任务租约的 Worker ID。
            now: 测试可注入的当前 UTC 时间。

        Returns:
            已进入 running 状态的后台任务。
        """
        started_at = now or utc_now()
        with open_application_session(self._session_factory) as session:
            repositories = create_repository_bundle(session)
            job = repositories.background_jobs.mark_running(
                job_id,
                worker_id=worker_id,
                started_at=started_at,
            )
            repositories.governance_runs.update_status(
                job.run_id,
                status="running",
                current_stage="background_worker",
            )
            return background_job_to_state(job)

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> WorkerLeaseState:
        """使用独立短事务续期当前 Worker 租约。

        Args:
            job_id: 当前执行的后台任务 ID。
            worker_id: 当前租约持有者的 Worker ID。
            lease_seconds: 从本次心跳开始计算的新租约时长。
            now: 测试可注入的当前 UTC 时间。

        Returns:
            已续期并脱离 Session 的 Worker 租约状态。
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于零")
        heartbeat_at = now or utc_now()
        expires_at = heartbeat_at + timedelta(seconds=lease_seconds)
        with open_application_session(self._session_factory) as session:
            lease = create_repository_bundle(session).worker_leases.heartbeat(
                job_id,
                worker_id=worker_id,
                heartbeat_at=heartbeat_at,
                expires_at=expires_at,
            )
            return worker_lease_to_state(lease)

    def finish(
        self,
        job_id: str,
        *,
        worker_id: str,
        status: str,
        report_path: str | None = None,
        error_summary: str | None = None,
        pending_interrupt: PendingInterruptState | None = None,
        now: datetime | None = None,
    ) -> BackgroundJobState:
        """在一个事务中收口后台任务、治理运行并释放 Worker 租约。

        Args:
            job_id: 等待收口的后台任务 ID。
            worker_id: 当前租约持有者的 Worker ID。
            status: ``waiting_human``、``completed``、``partial`` 或 ``failed``。
            report_path: 可选治理报告路径。
            error_summary: 可选脱敏错误摘要。
            pending_interrupt: waiting_human 时必须保存的当前中断快照。
            now: 测试可注入的当前 UTC 时间。

        Returns:
            已收口并脱离 Session 的后台任务状态。
        """
        if status not in WORKER_RESULT_STATUSES:
            raise ValueError("status 不是允许的 Worker 收口状态")
        finished_at = now or utc_now()
        with open_application_session(self._session_factory) as session:
            repositories = create_repository_bundle(session)
            job = repositories.background_jobs.finish(
                job_id,
                worker_id=worker_id,
                status=status,
                finished_at=finished_at,
                report_path=report_path,
                error_summary=error_summary,
                pending_interrupt=(
                    dict(pending_interrupt) if pending_interrupt is not None else None
                ),
            )
            repositories.worker_leases.release(
                job_id,
                worker_id=worker_id,
                status="released",
                released_at=finished_at,
            )
            repositories.governance_runs.update_status(
                job.run_id,
                status=status,
                current_stage=("waiting_human" if status == "waiting_human" else "finished"),
                report_path=report_path,
                error_summary=error_summary,
                finished_at=finished_at if status in TERMINAL_JOB_STATUSES else None,
            )
            return background_job_to_state(job)

    def fail_or_requeue(
        self,
        job_id: str,
        *,
        worker_id: str,
        error_summary: str,
        retry_delay_seconds: float,
        now: datetime | None = None,
    ) -> BackgroundJobState:
        """根据剩余尝试次数重新入队或最终失败当前 Worker 的任务。

        Args:
            job_id: 当前执行失败的后台任务 ID。
            worker_id: 当前租约持有者的 Worker ID。
            error_summary: 不含业务正文和堆栈的失败摘要。
            retry_delay_seconds: 重新允许领取前的等待秒数。
            now: 测试可注入的当前 UTC 时间。

        Returns:
            已进入 queued 或 failed 状态的后台任务。
        """
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 不得为负数")
        failed_at = now or utc_now()
        with open_application_session(self._session_factory) as session:
            repositories = create_repository_bundle(session)
            job = repositories.background_jobs.get_required(job_id)
            if job.current_worker_id != worker_id:
                raise RuntimeError("只有当前租约 Worker 可以处理任务失败")
            is_resume_attempt = (
                isinstance(job.resume_state, dict) and job.resume_state.get("status") == "pending"
            )
            effective_attempt_count = job.attempt_count + int(is_resume_attempt)
            if effective_attempt_count < job.max_attempts:
                record = repositories.background_jobs.requeue(
                    job_id,
                    available_at=failed_at + timedelta(seconds=retry_delay_seconds),
                    updated_at=failed_at,
                    error_summary=error_summary,
                    increment_attempt_count=is_resume_attempt,
                )
                run_status = "queued"
                run_stage = (
                    "background_resume_retry_queued"
                    if is_resume_attempt
                    else "background_retry_queued"
                )
            else:
                record = repositories.background_jobs.fail_expired(
                    job_id,
                    failed_at=failed_at,
                    error_summary=error_summary,
                    increment_attempt_count=is_resume_attempt,
                )
                run_status = "failed"
                run_stage = "background_failed"
            repositories.worker_leases.release(
                job_id,
                worker_id=worker_id,
                status="released",
                released_at=failed_at,
            )
            repositories.governance_runs.update_status(
                job.run_id,
                status=run_status,
                current_stage=run_stage,
                error_summary=error_summary,
                finished_at=failed_at if run_status == "failed" else None,
            )
            return background_job_to_state(record)

    def requeue_expired(
        self,
        *,
        now: datetime | None = None,
        limit: int = 500,
    ) -> int:
        """扫描过期 active 租约并重新入队或最终失败对应任务。

        Args:
            now: 测试可注入的当前 UTC 时间。
            limit: 单次扫描允许处理的最大租约数量。

        Returns:
            本次事务实际处理的过期租约数量。
        """
        recovered_at = now or utc_now()
        processed = 0
        with open_application_session(self._session_factory) as session:
            repositories = create_repository_bundle(session)
            expired_leases = repositories.worker_leases.list_expired_active(
                expired_before=recovered_at,
                limit=limit,
            )
            for lease in expired_leases:
                job = repositories.background_jobs.get(lease.job_id)
                if job is None:
                    continue
                summary = "Worker 租约过期，任务执行进程可能已经异常退出。"
                if job.status in TERMINAL_JOB_STATUSES or job.status == "waiting_human":
                    repositories.worker_leases.release(
                        lease.job_id,
                        worker_id=lease.worker_id,
                        status="expired",
                        released_at=recovered_at,
                    )
                    processed += 1
                    continue
                is_resume_attempt = (
                    isinstance(job.resume_state, dict)
                    and job.resume_state.get("status") == "pending"
                )
                effective_attempt_count = job.attempt_count + int(is_resume_attempt)
                if effective_attempt_count < job.max_attempts:
                    repositories.background_jobs.requeue(
                        job.job_id,
                        available_at=recovered_at,
                        updated_at=recovered_at,
                        error_summary=summary,
                        increment_attempt_count=is_resume_attempt,
                    )
                    run_status = "queued"
                    run_stage = (
                        "worker_resume_lease_requeued"
                        if is_resume_attempt
                        else "worker_lease_requeued"
                    )
                else:
                    repositories.background_jobs.fail_expired(
                        job.job_id,
                        failed_at=recovered_at,
                        error_summary=summary,
                        increment_attempt_count=is_resume_attempt,
                    )
                    run_status = "failed"
                    run_stage = "worker_lease_failed"
                repositories.worker_leases.release(
                    lease.job_id,
                    worker_id=lease.worker_id,
                    status="expired",
                    released_at=recovered_at,
                )
                repositories.governance_runs.update_status(
                    job.run_id,
                    status=run_status,
                    current_stage=run_stage,
                    error_summary=summary,
                    finished_at=recovered_at if run_status == "failed" else None,
                )
                processed += 1
        return processed
