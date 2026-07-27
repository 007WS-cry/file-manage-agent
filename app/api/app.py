from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app import __version__
from app.api.routes.runs import router as runs_router
from app.api.routes.schedules import router as schedules_router
from app.api.schemas import HealthResponse
from app.runtime.job_queue import JobQueue
from app.runtime.scheduler import SchedulerService
from app.storage.database import (
    APPLICATION_DATABASE_URL_ENV,
    get_application_database_backend,
    resolve_application_database_target,
)

"""本模块创建 FastAPI 应用，并在 lifespan 内管理持久化后台任务队列连接池。"""


# API 和 Worker 默认共享的 SQLite checkpoint 环境变量名称。
CHECKPOINT_PATH_ENV = "FILE_GOVERNANCE_CHECKPOINT_PATH"

# 未通过环境变量覆盖时使用的默认 SQLite checkpoint 路径。
DEFAULT_RUNTIME_CHECKPOINT_PATH = Path(
    ".artifacts/checkpoints/file-governance-background.sqlite3"
)


def create_app(
    *,
    database_path: str | Path | None = None,
    database_url: str | None = None,
    checkpoint_path: str | Path | None = None,
) -> FastAPI:
    """创建具有独立队列生命周期和固定运行路由的 FastAPI 应用。

    Args:
        database_path: 可选 SQLite 应用数据库路径。
        database_url: 可选 PostgreSQL 或 SQLite SQLAlchemy URL；优先级高于环境变量。
        checkpoint_path: 可选后台 checkpoint 路径；省略时读取环境变量或默认值。

    Returns:
        已注册健康检查、后台运行和定时计划管理路由的 FastAPI 应用。
    """
    resolved_database_target = resolve_application_database_target(
        database_url=database_url,
        database_path=database_path,
    )
    if get_application_database_backend(resolved_database_target) == "postgresql":
        os.environ[APPLICATION_DATABASE_URL_ENV] = str(resolved_database_target)
    resolved_checkpoint_path = Path(
        checkpoint_path
        or os.environ.get(
            CHECKPOINT_PATH_ENV,
            str(DEFAULT_RUNTIME_CHECKPOINT_PATH),
        )
    ).expanduser().resolve()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """在 API 启动时创建队列，并在进程关闭时释放连接池。

        Args:
            application: 当前 FastAPI 应用实例。

        Yields:
            队列可用期间的应用生命周期控制权。
        """
        queue = JobQueue(resolved_database_target)
        application.state.job_queue = queue
        application.state.checkpoint_path = str(resolved_checkpoint_path)
        scheduler_service = SchedulerService(
            resolved_database_target,
            resolved_checkpoint_path,
            queue=queue,
        )
        application.state.scheduler_service = scheduler_service
        try:
            yield
        finally:
            scheduler_service.close()
            queue.close()

    application = FastAPI(
        title="File Governance Background API",
        version=__version__,
        lifespan=lifespan,
    )
    application.include_router(runs_router)
    application.include_router(schedules_router)

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """返回不访问业务文件或数据库内容的进程存活信息。

        Returns:
            固定 ok 状态和当前应用版本。
        """
        return HealthResponse(status="ok", version=__version__)

    return application


# Uvicorn 默认导入的 FastAPI 应用实例。
app = create_app()
