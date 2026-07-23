from __future__ import annotations

import sqlalchemy as sa

from alembic import op

"""本迁移首次创建治理运行、Memory、上下文摘要、工具审计和人工审核五张应用表。"""


# 当前 0.5.1 应用数据库首个迁移版本标识。
revision = "0001_application_tables"

# 首个迁移没有上一版本。
down_revision = None

# 当前迁移不属于并行分支。
branch_labels = None

# 当前迁移没有额外依赖。
depends_on = None


def upgrade() -> None:
    """创建 0.5.1 应用数据库的五张基础表、约束和索引。"""
    op.create_table(
        "governance_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_stage", sa.String(length=128), nullable=False),
        sa.Column("request_summary", sa.JSON(), nullable=False),
        sa.Column("report_path", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('created', 'queued', 'running', 'waiting_human', "
            "'completed', 'partial', 'failed')",
            name="ck_governance_runs_status_allowed",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            name="pk_governance_runs",
        ),
    )
    op.create_index(
        "ix_governance_runs_thread_id",
        "governance_runs",
        ["thread_id"],
        unique=False,
    )

    op.create_table(
        "memory_items",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("namespace", sa.String(length=256), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("structured_data", sa.JSON(), nullable=False),
        sa.Column("artifact_refs", sa.JSON(), nullable=False),
        sa.Column("source_run_id", sa.String(length=64), nullable=False),
        sa.Column("confirmed_by_human", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope IN ('short_term', 'long_term')",
            name="ck_memory_items_scope_allowed",
        ),
        sa.CheckConstraint(
            "kind IN ('stage_summary', 'confirmed_version_choice', "
            "'reliable_evidence_relation', 'governance_preference')",
            name="ck_memory_items_kind_allowed",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memory_items_confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["governance_runs.run_id"],
            name="fk_memory_items_source_run_id_governance_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_items"),
    )
    op.create_index(
        "ix_memory_items_kind",
        "memory_items",
        ["kind"],
        unique=False,
    )
    op.create_index(
        "ix_memory_items_namespace",
        "memory_items",
        ["namespace"],
        unique=False,
    )
    op.create_index(
        "ix_memory_items_source_run_id",
        "memory_items",
        ["source_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_items_namespace_kind_created",
        "memory_items",
        ["namespace", "kind", "created_at"],
        unique=False,
    )

    op.create_table(
        "context_summaries",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("artifact_refs", sa.JSON(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("compaction_index", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stage IN ('after_inventory', 'after_evidence')",
            name="ck_context_summaries_stage_allowed",
        ),
        sa.CheckConstraint(
            "estimated_tokens >= 0",
            name="ck_context_summaries_estimated_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "compaction_index >= 1",
            name="ck_context_summaries_compaction_index_positive",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["governance_runs.run_id"],
            name="fk_context_summaries_run_id_governance_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_context_summaries"),
        sa.UniqueConstraint(
            "run_id",
            "compaction_index",
            name="uq_context_summaries_run_compaction",
        ),
    )
    op.create_index(
        "ix_context_summaries_run_id",
        "context_summaries",
        ["run_id"],
        unique=False,
    )

    op.create_table(
        "tool_call_audits",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=160), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "output_summary",
            sa.Text(),
            server_default="",
            nullable=False,
        ),
        sa.Column("output_ref", sa.Text(), nullable=True),
        sa.Column(
            "output_size_bytes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('success', 'failed', 'timeout')",
            name="ck_tool_call_audits_status_allowed",
        ),
        sa.CheckConstraint(
            "output_size_bytes >= 0",
            name="ck_tool_call_audits_output_size_nonnegative",
        ),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name="ck_tool_call_audits_duration_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["governance_runs.run_id"],
            name="fk_tool_call_audits_run_id_governance_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tool_call_audits"),
    )
    op.create_index(
        "ix_tool_call_audits_run_id",
        "tool_call_audits",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "ix_tool_call_audits_task_id",
        "tool_call_audits",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "ix_tool_call_audits_tool_name",
        "tool_call_audits",
        ["tool_name"],
        unique=False,
    )

    op.create_table(
        "human_reviews",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("group_id", sa.String(length=128), nullable=False),
        sa.Column("selected_file_id", sa.String(length=128), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "reviewer_label",
            sa.String(length=128),
            server_default="user",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["governance_runs.run_id"],
            name="fk_human_reviews_run_id_governance_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_human_reviews"),
        sa.UniqueConstraint(
            "run_id",
            "group_id",
            name="uq_human_reviews_run_group",
        ),
    )
    op.create_index(
        "ix_human_reviews_group_id",
        "human_reviews",
        ["group_id"],
        unique=False,
    )
    op.create_index(
        "ix_human_reviews_run_id",
        "human_reviews",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    """按外键依赖逆序删除 0.5.1 应用数据库基础表。"""
    op.drop_index("ix_human_reviews_run_id", table_name="human_reviews")
    op.drop_index("ix_human_reviews_group_id", table_name="human_reviews")
    op.drop_table("human_reviews")

    op.drop_index("ix_tool_call_audits_tool_name", table_name="tool_call_audits")
    op.drop_index("ix_tool_call_audits_task_id", table_name="tool_call_audits")
    op.drop_index("ix_tool_call_audits_run_id", table_name="tool_call_audits")
    op.drop_table("tool_call_audits")

    op.drop_index("ix_context_summaries_run_id", table_name="context_summaries")
    op.drop_table("context_summaries")

    op.drop_index(
        "ix_memory_items_namespace_kind_created",
        table_name="memory_items",
    )
    op.drop_index("ix_memory_items_source_run_id", table_name="memory_items")
    op.drop_index("ix_memory_items_namespace", table_name="memory_items")
    op.drop_index("ix_memory_items_kind", table_name="memory_items")
    op.drop_table("memory_items")

    op.drop_index("ix_governance_runs_thread_id", table_name="governance_runs")
    op.drop_table("governance_runs")
