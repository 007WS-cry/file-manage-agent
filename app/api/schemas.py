from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.state.models import BackgroundJobState

"""本模块定义 HTTP API 的请求与响应 Schema，并隔离 LangGraph 完整内部状态。"""


class RunSubmissionRequest(BaseModel):
    """提交一次持久化后台文件治理运行的 HTTP 请求。"""

    model_config = ConfigDict(extra="forbid")
    # 拒绝未知顶层字段，避免调用方误以为未识别参数已经生效。

    execution_mode: Literal["background"] = "background"
    # 第一批 HTTP API 只允许立即返回的后台执行方式。

    max_attempts: int = Field(default=3, ge=1, le=20)
    # Worker 异常退出或执行失败后允许的总领取次数。

    payload: dict[str, Any]
    # 包含 request、workspace 和可选运行配置的治理请求信封。


class RunSubmissionResponse(BaseModel):
    """后台请求成功入队后立即返回的最小接收凭证。"""

    model_config = ConfigDict(extra="forbid")
    # 响应只能包含当前协议声明的白名单字段。

    run_id: str
    # 文件治理运行唯一 ID。

    job_id: str
    # 持久化后台队列任务唯一 ID。

    thread_id: str
    # 跨进程恢复 LangGraph 使用的线程 ID。

    status: Literal["queued"]
    # API 返回时任务已经持久化入队但尚未保证开始执行。


class BackgroundJobResponse(BaseModel):
    """HTTP API 可公开的后台任务生命周期摘要。"""

    model_config = ConfigDict(extra="forbid")
    # 响应不得意外包含 request_payload、文档正文或 Worker 内部对象。

    job_id: str
    # 后台任务唯一 ID。

    run_id: str
    # 对应治理运行唯一 ID。

    thread_id: str
    # 对应 LangGraph 线程 ID。

    trigger_source: Literal["manual", "cron"]
    # 后台任务由手动请求还是 Cron 计划创建。

    status: Literal[
        "queued",
        "leased",
        "running",
        "waiting_human",
        "completed",
        "partial",
        "failed",
    ]
    # 后台任务当前生命周期状态。

    current_worker_id: str | None
    # 当前持有任务租约的 Worker ID。

    attempt_count: int
    # Worker 已经领取并尝试执行的累计次数。

    max_attempts: int
    # 当前任务允许的最大尝试次数。

    available_at: str
    # 当前任务最早允许领取的时间。

    claimed_at: str | None
    # 最近一次领取任务的时间。

    started_at: str | None
    # 最近一次开始执行 LangGraph 的时间。

    report_path: str | None
    # 成功或部分成功后的报告路径。

    error_summary: str | None
    # 最近一次后台执行问题的脱敏摘要。

    created_at: str
    # 后台任务创建时间。

    updated_at: str
    # 后台任务最近一次状态变化时间。

    finished_at: str | None
    # 后台任务进入最终状态的时间。

    @classmethod
    def from_state(cls, job: BackgroundJobState) -> BackgroundJobResponse:
        """从内部后台任务状态创建不包含请求信封的 API 响应。

        Args:
            job: 持久化队列返回的完整后台任务状态。

        Returns:
            只保留生命周期、ID、报告和脱敏错误字段的响应对象。
        """
        return cls(
            job_id=job["id"],
            run_id=job["run_id"],
            thread_id=job["thread_id"],
            trigger_source=job["trigger_source"],
            status=job["status"],
            current_worker_id=job["current_worker_id"],
            attempt_count=job["attempt_count"],
            max_attempts=job["max_attempts"],
            available_at=job["available_at"],
            claimed_at=job["claimed_at"],
            started_at=job["started_at"],
            report_path=job["report_path"],
            error_summary=job["error_summary"],
            created_at=job["created_at"],
            updated_at=job["updated_at"],
            finished_at=job["finished_at"],
        )


class RunStatusResponse(BaseModel):
    """HTTP API 可公开的治理运行与可选后台任务组合状态。"""

    model_config = ConfigDict(extra="forbid")
    # 响应不得包含请求摘要、完整报告 Markdown 或 LangGraph checkpoint。

    run_id: str
    # 治理运行唯一 ID。

    thread_id: str
    # LangGraph Checkpointer 使用的线程 ID。

    status: str
    # 治理运行当前生命周期状态。

    current_stage: str
    # 当前执行或最近完成的治理阶段。

    report_path: str | None
    # 最终报告路径；尚未生成时为 None。

    error_summary: str | None
    # 可供调用方展示的脱敏错误摘要。

    created_at: str | None
    # 治理运行记录创建时间。

    started_at: str | None
    # LangGraph 实际开始执行时间。

    finished_at: str | None
    # 治理运行最终结束时间。

    updated_at: str | None
    # 治理运行记录最近更新时间。

    background_job: BackgroundJobResponse | None
    # 对应后台任务摘要；非后台运行时为 None。


class HealthResponse(BaseModel):
    """API 进程存活检查使用的固定响应。"""

    model_config = ConfigDict(extra="forbid")
    # 健康响应不接受或返回扩展字段。

    status: Literal["ok"]
    # API 进程已经完成应用创建并可接收请求。

    version: str
    # 当前服务公开的应用版本号。
