from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.state.models import BackgroundJobState, ScheduledJobState

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


class PendingInterruptResponse(BaseModel):
    """HTTP API 可公开的当前后台中断快照。"""

    model_config = ConfigDict(extra="forbid")
    # 响应只能包含恢复当前 checkpoint 所需的最小中断字段。

    interrupt_id: str
    # LangGraph 为当前中断生成的稳定 ID。

    kind: Literal["file_governance_review", "error_recovery"]
    # 当前中断所遵循的人审或错误恢复协议。

    payload: dict[str, Any]
    # 供调用方构造恢复值的受控中断载荷。

    created_at: str
    # Worker 持久化当前中断的时间。


class BackgroundResumeRequest(BaseModel):
    """通过当前 interrupt_id 提交一次幂等后台人工恢复的 HTTP 请求。"""

    model_config = ConfigDict(extra="forbid")
    # 拒绝未知字段，避免恢复参数被静默忽略。

    request_id: str = Field(min_length=1, max_length=128)
    # 调用方提供的幂等键；同一次操作重试必须保持不变。

    interrupt_id: str = Field(min_length=1, max_length=256)
    # 必须与状态接口当前公开的中断 ID 完全一致。

    kind: Literal["file_governance_review", "error_recovery"]
    # 恢复值所遵循的固定协议类型。

    value: dict[str, Any]
    # 将由 Worker 传给 Command(resume=...) 的协议对象。


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
        "resume_queued",
        "leased",
        "running",
        "waiting_human",
        "completed",
        "partial",
        "failed",
    ]
    # 后台任务当前生命周期状态，包括独立的人工恢复排队状态。

    current_worker_id: str | None
    # 当前持有任务租约的 Worker ID。

    attempt_count: int
    # 首次执行和异常重试累计次数；正常人工恢复不增加该值。

    max_attempts: int
    # 当前任务允许的最大尝试次数。

    resume_count: int
    # 已成功应用的人工恢复次数。

    pending_interrupt: PendingInterruptResponse | None
    # 当前等待调用方恢复的中断；其他状态通常为 None。

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
            resume_count=job["resume_count"],
            pending_interrupt=job["pending_interrupt"],
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

    report_available: bool
    # 当前后台运行是否已经登记可供下载的报告路径。

    report_url: str | None
    # 受控报告下载接口；尚无报告时为 None。

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


class ScheduleCreateRequest(BaseModel):
    """创建一项持久化 Cron 计划的 HTTP 请求。"""

    model_config = ConfigDict(extra="forbid")
    # 拒绝未声明字段，避免调用方误以为动态调度参数已经生效。

    name: str = Field(min_length=1, max_length=160)
    # 面向用户展示的计划名称。

    cron_expression: str = Field(min_length=1, max_length=160)
    # 标准五段 crontab 表达式。

    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    # 解释 Cron 表达式的 IANA 时区名称。

    enabled: bool = True
    # 创建后是否允许独立 Scheduler 注册和触发。

    payload: dict[str, Any]
    # Cron 触发时复制到后台队列的治理请求模板。


class ScheduleResponse(BaseModel):
    """HTTP API 可公开的持久化 Cron 计划和最近运行摘要。"""

    model_config = ConfigDict(extra="forbid")
    # 响应不返回治理请求模板、文档路径信封或 APScheduler 内部对象。

    schedule_id: str
    # 持久化计划唯一 ID。

    name: str
    # 面向用户展示的计划名称。

    cron_expression: str
    # 已通过调度层校验的五段 Cron 表达式。

    timezone: str
    # 解释 Cron 的 IANA 时区名称。

    enabled: bool
    # 独立 Scheduler 是否应注册当前计划。

    last_triggered_at: str | None
    # 最近一次成功创建后台任务的时间。

    last_run_id: str | None
    # 最近一次 Cron 触发创建的治理运行 ID。

    next_run_at: str | None
    # Scheduler 最近计算的下一次预计触发时间。

    last_error: str | None
    # 最近一次注册或入队失败的脱敏摘要。

    created_at: str
    # 计划创建时间。

    updated_at: str
    # 计划最近一次修改或触发时间。

    @classmethod
    def from_state(cls, schedule: ScheduledJobState) -> ScheduleResponse:
        """从内部计划状态创建不包含请求模板的 API 响应。

        Args:
            schedule: SchedulerService 返回的完整持久化计划状态。

        Returns:
            只保留调度规则、生命周期和最近运行事实的响应对象。
        """
        return cls(
            schedule_id=schedule["id"],
            name=schedule["name"],
            cron_expression=schedule["cron_expression"],
            timezone=schedule["timezone"],
            enabled=schedule["enabled"],
            last_triggered_at=schedule["last_triggered_at"],
            last_run_id=schedule["last_run_id"],
            next_run_at=schedule["next_run_at"],
            last_error=schedule["last_error"],
            created_at=schedule["created_at"],
            updated_at=schedule["updated_at"],
        )


class HealthResponse(BaseModel):
    """API 进程存活检查使用的固定响应。"""

    model_config = ConfigDict(extra="forbid")
    # 健康响应不接受或返回扩展字段。

    status: Literal["ok"]
    # API 进程已经完成应用创建并可接收请求。

    version: str
    # 当前服务公开的应用版本号。
