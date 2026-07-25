from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError

from app.api.schemas import (
    BackgroundJobResponse,
    RunStatusResponse,
    RunSubmissionRequest,
    RunSubmissionResponse,
)
from app.runtime.dispatcher import create_background_submission
from app.runtime.job_queue import JobQueue

"""本模块提供后台治理运行的提交、按 run_id 查询和按 job_id 查询路由。"""


# 所有运行接口共享的固定 URL 前缀和 OpenAPI 标签。
router = APIRouter(prefix="/runs", tags=["runs"])


def get_job_queue(request: Request) -> JobQueue:
    """从 FastAPI 应用状态读取已经初始化的持久化队列。

    Args:
        request: 当前 HTTP 请求及其应用状态。

    Returns:
        应用 lifespan 创建并在关闭时统一释放的 JobQueue。

    Raises:
        HTTPException: API 尚未完成队列初始化时返回 503。
    """
    queue = getattr(request.app.state, "job_queue", None)
    if not isinstance(queue, JobQueue):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="后台任务队列尚未初始化",
        )
    return queue


@router.post(
    "",
    response_model=RunSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_background_run(
    submission: RunSubmissionRequest,
    request: Request,
) -> RunSubmissionResponse:
    """持久化提交后台治理任务并立即返回 run_id 与 job_id。

    Args:
        submission: 已通过 Pydantic 校验的后台提交请求。
        request: 用于读取共享队列和 checkpoint 路径的 HTTP 请求。

    Returns:
        状态固定为 queued 的后台任务接收凭证。

    Raises:
        HTTPException: 请求信封不合法时返回 422，数据库不可用时返回 503。
    """
    queue = get_job_queue(request)
    checkpoint_path = request.app.state.checkpoint_path
    try:
        job = create_background_submission(
            submission.payload,
            queue=queue,
            checkpoint_path=checkpoint_path,
            max_attempts=submission.max_attempts,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="后台任务数据库不可用，请确认已经执行 Alembic 迁移。",
        ) from exc
    return RunSubmissionResponse(
        run_id=job["run_id"],
        job_id=job["id"],
        thread_id=job["thread_id"],
        status="queued",
    )


@router.get("/{run_id}", response_model=RunStatusResponse)
def get_run_status(run_id: str, request: Request) -> RunStatusResponse:
    """按照 run_id 查询治理运行和关联后台任务的脱敏状态。

    Args:
        run_id: 提交接口返回的治理运行 ID。
        request: 用于读取共享持久化队列的 HTTP 请求。

    Returns:
        治理运行摘要及可选后台任务摘要。

    Raises:
        HTTPException: run_id 不存在时返回 404。
    """
    queue = get_job_queue(request)
    run = queue.get_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="治理运行不存在",
        )
    job = queue.get_job_by_run_id(run_id)
    return RunStatusResponse(
        **run,
        background_job=BackgroundJobResponse.from_state(job) if job is not None else None,
    )


@router.get("/jobs/{job_id}", response_model=BackgroundJobResponse)
def get_background_job_status(
    job_id: str,
    request: Request,
) -> BackgroundJobResponse:
    """按照 job_id 查询后台队列任务的脱敏状态。

    Args:
        job_id: 提交接口返回的后台任务 ID。
        request: 用于读取共享持久化队列的 HTTP 请求。

    Returns:
        不包含持久化请求信封的后台任务摘要。

    Raises:
        HTTPException: job_id 不存在时返回 404。
    """
    queue = get_job_queue(request)
    job = queue.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="后台任务不存在",
        )
    return BackgroundJobResponse.from_state(job)
