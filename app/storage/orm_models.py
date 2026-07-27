from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

"""本模块定义应用数据库的十张 SQLAlchemy ORM 表，不负责创建或迁移表结构。"""


# 统一约束命名规则，使 Alembic 自动生成和回退迁移时可以稳定引用约束。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。

    Returns:
        带 UTC 时区信息的 ``datetime``，用于 ORM 的 Python 侧默认值。
    """
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """应用数据库全部 ORM 模型共享的声明式基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # 包含稳定约束命名规则的 SQLAlchemy MetaData。


class GovernanceRunModel(Base):
    """保存一次文件版本治理运行的持久化生命周期摘要。"""

    __tablename__ = "governance_runs"
    # 应用数据库中的固定表名。

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 治理运行唯一 ID，与顶层 RunState.run_id 一致。

    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # LangGraph Checkpointer 使用的线程 ID。

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # 运行状态，只允许使用治理生命周期白名单值。

    current_stage: Mapped[str] = mapped_column(String(128), nullable=False)
    # 当前正在执行或最近完成的主图阶段。

    request_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    # 脱敏后的请求范围摘要，不得保存完整业务正文或凭据。

    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 最终治理报告路径；尚未生成报告时为 None。

    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 有长度边界的脱敏错误摘要；正常运行时为 None。

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )
    # 应用数据库首次创建该运行记录的时间。

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 治理图实际开始执行的时间。

    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 治理运行最终结束的时间；未结束时为 None。

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
    # 运行记录最近一次更新的时间。

    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'queued', 'running', 'recovering', 'waiting_human', "
            "'completed', 'partial', 'failed')",
            name="status_allowed",
        ),
    )
    # 限制运行状态，防止数据库保存未知生命周期值。


class BackgroundJobModel(Base):
    """保存一个可被独立 Worker 事务领取和跨进程恢复的后台治理任务。"""

    __tablename__ = "background_jobs"
    # 应用数据库中的固定表名。

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 后台任务唯一 ID，与治理运行和 Task DAG 标识分开。

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("governance_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # 当前后台任务对应的治理运行 ID。

    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 跨进程恢复 LangGraph checkpoint 使用的线程 ID。

    trigger_source: Mapped[str] = mapped_column(String(16), nullable=False)
    # 后台任务由手动请求还是 Cron 计划触发。

    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 后台任务在首次排队、恢复排队、领取、执行、中断和终结阶段的状态。

    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 已规范化的治理请求信封，不得包含密钥实际值或完整文档正文。

    current_worker_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )
    # 当前持有任务租约的 Worker ID；未领取时为 None。

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Worker 实际领取并尝试执行该任务的累计次数。

    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # Worker 崩溃或运行失败后允许重新领取的最大次数。

    resume_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    # 已成功应用的人工恢复次数，不计入异常重试次数。

    pending_interrupt: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 当前等待 API 恢复的中断 ID、协议类型和最小载荷。

    resume_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 最近一次幂等恢复请求；等待执行时包含恢复值，应用后仅保留元数据。

    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )
    # 当前任务最早允许再次领取的时间。

    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 最近一次被 Worker 成功领取的时间。

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 最近一次开始执行 LangGraph 的时间。

    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 成功或部分成功后生成的治理报告路径。

    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 最近一次后台执行失败或租约过期的脱敏摘要。

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )
    # 后台任务首次入队时间。

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
    # 后台任务最近一次状态变化时间。

    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 后台任务进入最终状态的时间。

    __table_args__ = (
        CheckConstraint(
            "trigger_source IN ('manual', 'cron')",
            name="trigger_source_allowed",
        ),
        CheckConstraint(
            "status IN ('queued', 'resume_queued', 'leased', 'running', 'waiting_human', "
            "'completed', 'partial', 'failed')",
            name="status_allowed",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts "
            "AND resume_count >= 0",
            name="attempt_counts_valid",
        ),
        Index(
            "ix_background_jobs_claimable",
            "status",
            "available_at",
            "created_at",
        ),
    )
    # 限制来源、生命周期、异常尝试和恢复计数，并优化 Worker 的领取查询。


class ScheduledJobModel(Base):
    """保存一项后续可由 APScheduler 注册和恢复的 Cron 计划。"""

    __tablename__ = "scheduled_jobs"
    # 应用数据库中的固定表名。

    schedule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 定时计划唯一 ID。

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # 面向用户展示的计划名称。

    cron_expression: Mapped[str] = mapped_column(String(160), nullable=False)
    # 经过调度层校验的 Cron 表达式。

    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    # 解释 Cron 表达式所使用的时区名称。

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    # 当前计划是否允许继续触发后台任务。

    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 计划触发时复制使用的治理请求模板。

    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 最近一次实际触发计划的时间。

    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 最近一次触发创建的治理运行 ID。

    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Scheduler 最近计算的下一次预计触发时间。

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 最近一次注册或触发失败的脱敏摘要。

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )
    # 定时计划创建时间。

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
    # 定时计划最近一次修改时间。


class WorkerLeaseModel(Base):
    """保存一个后台任务当前或最近一次 Worker 执行租约。"""

    __tablename__ = "worker_leases"
    # 应用数据库中的固定表名。

    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("background_jobs.job_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 被租约锁定的后台任务 ID；每个任务只保留当前或最近一次租约。

    lease_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 当前租约唯一 ID，每次重新领取都会替换为新值。

    worker_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 持有当前租约的 Worker 唯一 ID。

    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # 租约当前处于有效、主动释放或已经过期状态。

    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Worker 成功领取任务的时间。

    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Worker 最近一次续租心跳时间。

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    # 超过该时间未续租时允许其他 Worker 重新领取。

    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Worker 完成、暂停、失败或租约过期后的释放时间。

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
    # 租约最近一次心跳或状态变化时间。

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'released', 'expired')",
            name="status_allowed",
        ),
        Index(
            "ix_worker_leases_expiration",
            "status",
            "expires_at",
        ),
    )
    # 限制租约生命周期，并优化过期租约扫描。


class MemoryItemModel(Base):
    """保存结构化短期或长期治理 Memory，不保存完整业务正文。"""

    __tablename__ = "memory_items"
    # 应用数据库中的固定表名。

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Memory 条目唯一 ID。

    namespace: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    # 隔离不同目录、用户或业务空间的长期 Memory 命名空间。

    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    # Memory 范围，只允许 short_term 或 long_term。

    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 阶段摘要、确认版本、可靠证据关系或治理偏好等 Memory 类型。

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # 有长度上限的治理结论摘要，禁止存放完整文档正文。

    structured_data: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    # 文件哈希、版本组 ID 和偏好参数等结构化数据。

    artifact_refs: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    # 支撑该 Memory 的受控产物引用。

    source_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("governance_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 产生该 Memory 的治理运行 ID。

    confirmed_by_human: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    # 该 Memory 是否来自用户明确确认。

    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    # Memory 结论置信度，范围为 0.0 到 1.0。

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )
    # Memory 条目创建时间。

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
    # Memory 条目最近一次更新时间。

    __table_args__ = (
        CheckConstraint(
            "scope IN ('short_term', 'long_term')",
            name="scope_allowed",
        ),
        CheckConstraint(
            "kind IN ('stage_summary', 'confirmed_version_choice', "
            "'reliable_evidence_relation', 'governance_preference')",
            name="kind_allowed",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="confidence_range",
        ),
        Index(
            "ix_memory_items_namespace_kind_created",
            "namespace",
            "kind",
            "created_at",
        ),
    )
    # 限制 Memory 范围、类型、置信度，并优化命名空间内的历史读取。


class ContextSummaryModel(Base):
    """保存一次 Context Compact 产生的有界上下文摘要。"""

    __tablename__ = "context_summaries"
    # 应用数据库中的固定表名。

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Context Summary 唯一 ID。

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("governance_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 该摘要所属的治理运行 ID。

    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    # 触发压缩的阶段，例如 after_inventory 或 after_evidence。

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # 压缩后的有界上下文摘要。

    artifact_refs: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    # 被移出上下文的大型输出产物引用。

    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    # 压缩完成后估算的上下文 Token 数。

    compaction_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 当前运行内从一开始递增的压缩序号。

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )
    # Context Summary 创建时间。

    __table_args__ = (
        CheckConstraint(
            "stage IN ('after_inventory', 'after_evidence')",
            name="stage_allowed",
        ),
        CheckConstraint(
            "estimated_tokens >= 0",
            name="estimated_tokens_nonnegative",
        ),
        CheckConstraint(
            "compaction_index >= 1",
            name="compaction_index_positive",
        ),
        UniqueConstraint(
            "run_id",
            "compaction_index",
            name="uq_context_summaries_run_compaction",
        ),
    )
    # 限制压缩阶段和数值范围，并防止同一运行重复保存相同序号。


class ToolCallAuditModel(Base):
    """保存普通 Python Tool 调用的脱敏审计信息。"""

    __tablename__ = "tool_call_audits"
    # 应用数据库中的固定表名。

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # 工具调用审计唯一 ID。

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("governance_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 工具调用所属的治理运行 ID。

    task_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    # 工具调用所属 Task ID；生命周期工具没有 Task 时为 None。

    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 被调用的固定工具名称。

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # 工具调用成功、失败或超时状态。

    output_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 有长度上限的脱敏输出摘要。

    output_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 大型输出转存后的受控产物引用。

    output_size_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    # 工具原始输出大小，单位为字节。

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 工具调用耗时，单位为毫秒。

    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 工具失败或超时时的异常类型。

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 已脱敏的简短错误信息。

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )
    # 工具调用审计创建时间。

    __table_args__ = (
        CheckConstraint(
            "status IN ('success', 'failed', 'timeout')",
            name="status_allowed",
        ),
        CheckConstraint(
            "output_size_bytes >= 0",
            name="output_size_nonnegative",
        ),
        CheckConstraint(
            "duration_ms >= 0",
            name="duration_nonnegative",
        ),
    )
    # 限制工具调用状态和非负统计值。


class HumanReviewModel(Base):
    """保存用户对某个版本组作出的主版本确认记录。"""

    __tablename__ = "human_reviews"
    # 应用数据库中的固定表名。

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # 人工审核记录唯一 ID。

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("governance_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 人工审核所属的治理运行 ID。

    group_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 用户确认的版本组 ID。

    selected_file_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # 用户最终确认的主版本文件 ID。

    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 用户提供的补充说明；未提供时为 None。

    reviewer_label: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="user",
    )
    # 脱敏审核者标签，默认记录为 user。

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )
    # 人工审核记录创建时间。

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "group_id",
            name="uq_human_reviews_run_group",
        ),
    )
    # 同一次治理运行中的同一版本组只保存一条最终确认记录。


class NodeExecutionRecordModel(Base):
    """保存一个可幂等复用、有限重试或审计的图节点执行记录。"""

    __tablename__ = "node_execution_records"
    # 应用数据库中的固定表名。

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # 节点幂等键，与 NodeExecutionRecord.id 一致。

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("governance_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 节点执行所属治理运行 ID。

    task_execution_id: Mapped[str | None] = mapped_column(
        String(192),
        nullable=True,
        index=True,
    )
    # 所属逻辑 Task 的稳定执行 ID；生命周期节点可以为 None。

    task_id: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        index=True,
    )
    # 所属 Task ID；Task DAG 创建前或生命周期节点可以为 None。

    stage: Mapped[str] = mapped_column(String(128), nullable=False)
    # 节点所属主流程阶段或子图名称。

    node_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 实际执行的函数节点名称。

    input_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    # 只根据稳定输入事实计算的摘要，用于拒绝错误结果复用。

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # 节点等待、运行、成功、失败或复用状态。

    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    # 节点累计执行次数，包含第一次执行。

    state_update_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 成功状态更新的受控 JSON 产物引用。

    result_refs: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    # 节点产生的受控业务产物引用。

    result_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 状态更新及结果引用的完整性摘要。

    last_error_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )
    # 最近一次失败对应的 ErrorRecord ID，不保存完整错误或堆栈。

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    # 第一次开始节点执行的时间。

    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 节点成功、失败或复用完成的时间。

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
    # 节点执行记录最近一次更新的时间。

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'reused')",
            name="status_allowed",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="attempt_count_nonnegative",
        ),
        Index(
            "ix_node_execution_records_run_status_updated",
            "run_id",
            "status",
            "updated_at",
        ),
    )
    # 限制节点状态和执行次数，并优化一次运行内的恢复查询。


class ErrorRecoveryRecordModel(Base):
    """保存一个错误在有限重试、降级和人工恢复中的持久化状态。"""

    __tablename__ = "error_recovery_records"
    # 应用数据库中的固定表名。

    record_id: Mapped[str] = mapped_column(String(320), primary_key=True)
    # 持久化记录 ID，由 run_id 和 ErrorRecord.id 确定性组合。

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("governance_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 错误所属治理运行 ID。

    error_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 顶层 ErrorRecord 的稳定错误 ID。

    task_id: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        index=True,
    )
    # 错误关联的 Task ID；Task DAG 创建前可以为 None。

    node_execution_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey(
            "node_execution_records.idempotency_key",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )
    # 错误关联的节点幂等执行记录；对应记录删除后置为 None。

    stage: Mapped[str] = mapped_column(String(128), nullable=False)
    # 错误发生的主流程阶段或子图名称。

    node_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 实际产生错误的函数节点名称。

    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 由 0.6.1 恢复策略识别的固定错误类别。

    exception_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 已脱敏异常类型；规则错误可以为 None。

    message: Mapped[str] = mapped_column(Text, nullable=False)
    # 可供审计和报告展示的脱敏错误说明。

    related_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 错误关联文件 ID；非文件错误时为 None。

    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 当前错误策略是否允许自动重试。

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 已经执行的额外重试次数。

    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 当前策略允许的最大额外重试次数。

    action: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    # 当前或最近一次确定性恢复动作。

    fallback: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 重试不可用或耗尽后采用的安全降级动作。

    requires_human: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    # 自动恢复不足时是否允许请求人工输入。

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # 错误等待、重试、降级、人工等待、恢复或失败状态。

    fatal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 兼容顶层 ErrorRecord 的致命错误标记。

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    # 错误首次捕获时间。

    recovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 错误完成恢复的时间；尚未恢复时为 None。

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
    # 恢复记录最近一次更新的时间。

    __table_args__ = (
        CheckConstraint(
            "category IN ('filesystem', 'parse', 'comparison', 'evidence', "
            "'llm', 'validation', 'protocol', 'prompt', 'hook', 'memory', "
            "'skill', 'context', 'database', 'checkpoint', 'worktree', 'mcp', "
            "'timeout', 'unknown')",
            name="category_allowed",
        ),
        CheckConstraint(
            "retry_count >= 0 AND max_retries >= 0 AND retry_count <= max_retries",
            name="retry_counts_valid",
        ),
        CheckConstraint(
            "action IN ('none', 'retry', 'reuse_result', 'skip_file', "
            "'fallback', 'continue_partial', 'wait_human', 'abort')",
            name="action_allowed",
        ),
        CheckConstraint(
            "fallback IS NULL OR fallback IN ('skip_file', 'coordinator', "
            "'no_memory', 'default_skill', 'keep_context', 'partial_result')",
            name="fallback_allowed",
        ),
        CheckConstraint(
            "status IN ('pending', 'retrying', 'fallback_applied', "
            "'waiting_human', 'recovered', 'failed')",
            name="status_allowed",
        ),
        UniqueConstraint(
            "run_id",
            "error_id",
            name="uq_error_recovery_records_run_error",
        ),
        Index(
            "ix_error_recovery_records_run_status_updated",
            "run_id",
            "status",
            "updated_at",
        ),
    )
    # 限制类别、计数、动作与状态，并确保同一运行中的错误只保存一条恢复记录。
