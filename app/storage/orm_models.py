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

"""本模块定义 0.5.1 应用数据库的五张 SQLAlchemy ORM 表，不负责创建或迁移表结构。"""


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
            "status IN ('created', 'queued', 'running', 'waiting_human', "
            "'completed', 'partial', 'failed')",
            name="status_allowed",
        ),
    )
    # 限制运行状态，防止数据库保存未知生命周期值。


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
