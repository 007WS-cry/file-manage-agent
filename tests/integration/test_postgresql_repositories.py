from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.state.models import BackgroundJobState
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.repositories import create_repository_bundle

"""本文件验证 PostgreSQL Repository 事务、JSON 持久化和 SKIP LOCKED 并发领取。"""


def _build_background_job(index: int) -> BackgroundJobState:
    """构造可直接写入 PostgreSQL Repository 的最小后台任务状态。

    Args:
        index: 用于稳定区分同一次测试中两个候选任务的序号。

    Returns:
        符合后台入队约束且不包含数据库凭据的任务状态。
    """
    created_at = datetime.now(timezone.utc).isoformat()
    identity = uuid4().hex
    return BackgroundJobState(
        id=f"postgres-job-{index}-{identity}",
        run_id=f"postgres-run-{index}-{identity}",
        thread_id=f"postgres-thread-{index}-{identity}",
        trigger_source="manual",
        status="queued",
        request_payload={
            "application_database": {
                "enabled": True,
                "backend": "postgresql",
                "database_path": None,
                "database_url_env": "FILE_GOVERNANCE_DATABASE_URL",
            }
        },
        current_worker_id=None,
        attempt_count=0,
        max_attempts=3,
        resume_count=0,
        pending_interrupt=None,
        resume=None,
        available_at=created_at,
        claimed_at=None,
        started_at=None,
        report_path=None,
        error_summary=None,
        created_at=created_at,
        updated_at=created_at,
        finished_at=None,
    )


def test_postgresql_repositories_skip_locked_candidates(
    postgresql_database_url: str,
) -> None:
    """两个未提交事务应通过 SKIP LOCKED 领取不同候选，并各自增加尝试次数。"""
    engine = create_application_engine(postgresql_database_url)
    session_factory = create_session_factory(engine)
    first_job = _build_background_job(1)
    second_job = _build_background_job(2)
    try:
        with open_application_session(session_factory) as session:
            repositories = create_repository_bundle(session)
            for job in (first_job, second_job):
                repositories.governance_runs.get_or_create_minimal(
                    job["run_id"],
                    thread_id=job["thread_id"],
                    current_stage="background_queued",
                    status="queued",
                )
                repositories.background_jobs.enqueue(job)

        first_session = session_factory()
        second_session = session_factory()
        try:
            first_session.begin()
            second_session.begin()
            first_claim = create_repository_bundle(
                first_session
            ).background_jobs.claim_next(
                worker_id="postgres-worker-one",
                claimed_at=datetime.now(timezone.utc),
            )
            second_claim = create_repository_bundle(
                second_session
            ).background_jobs.claim_next(
                worker_id="postgres-worker-two",
                claimed_at=datetime.now(timezone.utc),
            )

            assert first_claim is not None
            assert second_claim is not None
            assert first_claim.job_id != second_claim.job_id
            assert first_claim.attempt_count == 1
            assert second_claim.attempt_count == 1
            assert first_claim.request_payload["application_database"][
                "database_url_env"
            ] == "FILE_GOVERNANCE_DATABASE_URL"
            first_session.commit()
            second_session.commit()
        finally:
            first_session.close()
            second_session.close()
    finally:
        engine.dispose()
