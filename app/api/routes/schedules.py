from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.api.schemas import ScheduleCreateRequest, ScheduleResponse
from app.runtime.scheduler import SchedulerService

"""本模块提供 Cron 计划的创建、查看、启用和停用 HTTP 路由。"""


# 所有定时计划接口共享的固定 URL 前缀和 OpenAPI 标签。
router = APIRouter(prefix="/schedules", tags=["schedules"])


def get_scheduler_service(request: Request) -> SchedulerService:
    """从 FastAPI 应用状态读取未启动调度循环的计划管理服务。

    Args:
        request: 当前 HTTP 请求及其应用状态。

    Returns:
        lifespan 创建、仅用于计划持久化管理的 SchedulerService。

    Raises:
        HTTPException: API 尚未完成服务初始化时返回 503。
    """
    service = getattr(request.app.state, "scheduler_service", None)
    if not isinstance(service, SchedulerService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="定时计划服务尚未初始化",
        )
    return service


@router.post(
    "",
    response_model=ScheduleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_schedule(
    submission: ScheduleCreateRequest,
    request: Request,
) -> ScheduleResponse:
    """校验并持久化一项 Cron 计划，但不在 API 进程执行调度或 LangGraph。

    Args:
        submission: 已通过 Pydantic 校验的计划名称、Cron、时区和请求模板。
        request: 用于读取计划管理服务的 HTTP 请求。

    Returns:
        创建后的计划及其下一次预计触发时间。

    Raises:
        HTTPException: Cron、时区或请求模板非法时返回 422，数据库不可用时返回 503。
    """
    service = get_scheduler_service(request)
    try:
        schedule = service.create_schedule(
            name=submission.name,
            cron_expression=submission.cron_expression,
            timezone_name=submission.timezone,
            request_payload=submission.payload,
            enabled=submission.enabled,
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except IntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="定时计划 ID 冲突",
        ) from error
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="计划数据库不可用，请确认已经执行 Alembic 迁移。",
        ) from error
    return ScheduleResponse.from_state(schedule)


@router.get("", response_model=list[ScheduleResponse])
def list_schedules(request: Request) -> list[ScheduleResponse]:
    """列出全部启用和停用计划的脱敏摘要。

    Args:
        request: 用于读取计划管理服务的 HTTP 请求。

    Returns:
        不包含治理请求模板的计划响应列表。
    """
    schedules = get_scheduler_service(request).list_schedules()
    return [ScheduleResponse.from_state(schedule) for schedule in schedules]


@router.get("/{schedule_id}", response_model=ScheduleResponse)
def get_schedule(schedule_id: str, request: Request) -> ScheduleResponse:
    """按照计划 ID 查询调度规则和最近运行状态。

    Args:
        schedule_id: 创建接口返回的计划 ID。
        request: 用于读取计划管理服务的 HTTP 请求。

    Returns:
        对应计划的脱敏摘要。

    Raises:
        HTTPException: 计划不存在时返回 404。
    """
    schedule = get_scheduler_service(request).get_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="定时计划不存在",
        )
    return ScheduleResponse.from_state(schedule)


def _set_schedule_enabled(
    schedule_id: str,
    request: Request,
    *,
    enabled: bool,
) -> ScheduleResponse:
    """统一处理启用和停用计划的数据库状态变化。

    Args:
        schedule_id: 等待修改的计划 ID。
        request: 用于读取计划管理服务的 HTTP 请求。
        enabled: 目标启用状态。

    Returns:
        修改后的计划摘要。

    Raises:
        HTTPException: 计划不存在时返回 404。
    """
    service = get_scheduler_service(request)
    if service.get_schedule(schedule_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="定时计划不存在",
        )
    return ScheduleResponse.from_state(
        service.set_schedule_enabled(schedule_id, enabled=enabled)
    )


@router.post("/{schedule_id}/enable", response_model=ScheduleResponse)
def enable_schedule(schedule_id: str, request: Request) -> ScheduleResponse:
    """启用一项计划，等待独立 Scheduler 下一轮同步。

    Args:
        schedule_id: 等待启用的计划 ID。
        request: 用于读取计划管理服务的 HTTP 请求。

    Returns:
        enabled 为 True 且已计算 next_run_at 的计划摘要。
    """
    return _set_schedule_enabled(schedule_id, request, enabled=True)


@router.post("/{schedule_id}/disable", response_model=ScheduleResponse)
def disable_schedule(schedule_id: str, request: Request) -> ScheduleResponse:
    """停用一项计划，独立 Scheduler 下一轮同步后移除进程内 Job。

    Args:
        schedule_id: 等待停用的计划 ID。
        request: 用于读取计划管理服务的 HTTP 请求。

    Returns:
        enabled 为 False 且 next_run_at 为空的计划摘要。
    """
    return _set_schedule_enabled(schedule_id, request, enabled=False)
