from __future__ import annotations

import sqlalchemy as sa

from alembic import op

"""本迁移新增后台任务、定时计划和 Worker 租约三张持久化运行时表。"""


# 当前 0.7.1 后台运行基础设施迁移版本标识。
revision = "0003_background_runtime_tables"

# 当前迁移基于 0.7.0 错误恢复与节点幂等持久化版本。
down_revision = "0002_error_recovery_tables"

# 当前迁移不属于并行分支。
branch_labels = None

# 当前迁移没有额外依赖。
depends_on = None


def upgrade() -> None:
    """新增后台队列、Cron 计划和 Worker 租约表及其领取索引。"""
    op.create_table(
        "background_jobs",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("trigger_source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("current_worker_id", sa.String(length=128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_path", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts",
            name="ck_background_jobs_attempt_counts_valid",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'waiting_human', "
            "'completed', 'partial', 'failed')",
            name="ck_background_jobs_status_allowed",
        ),
        sa.CheckConstraint(
            "trigger_source IN ('manual', 'cron')",
            name="ck_background_jobs_trigger_source_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["governance_runs.run_id"],
            name="fk_background_jobs_run_id_governance_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_background_jobs"),
    )
    op.create_index(
        "ix_background_jobs_available_at",
        "background_jobs",
        ["available_at"],
        unique=False,
    )
    op.create_index(
        "ix_background_jobs_claimable",
        "background_jobs",
        ["status", "available_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_background_jobs_current_worker_id",
        "background_jobs",
        ["current_worker_id"],
        unique=False,
    )
    op.create_index(
        "ix_background_jobs_run_id",
        "background_jobs",
        ["run_id"],
        unique=True,
    )
    op.create_index(
        "ix_background_jobs_status",
        "background_jobs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_background_jobs_thread_id",
        "background_jobs",
        ["thread_id"],
        unique=False,
    )

    op.create_table(
        "scheduled_jobs",
        sa.Column("schedule_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("cron_expression", sa.String(length=160), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_id", sa.String(length=64), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("schedule_id", name="pk_scheduled_jobs"),
    )
    op.create_index(
        "ix_scheduled_jobs_enabled",
        "scheduled_jobs",
        ["enabled"],
        unique=False,
    )

    op.create_table(
        "worker_leases",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("lease_id", sa.String(length=64), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'released', 'expired')",
            name="ck_worker_leases_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["background_jobs.job_id"],
            name="fk_worker_leases_job_id_background_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_worker_leases"),
        sa.UniqueConstraint("lease_id", name="uq_worker_leases_lease_id"),
    )
    op.create_index(
        "ix_worker_leases_expiration",
        "worker_leases",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_worker_leases_expires_at",
        "worker_leases",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_worker_leases_status",
        "worker_leases",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_worker_leases_worker_id",
        "worker_leases",
        ["worker_id"],
        unique=False,
    )


def downgrade() -> None:
    """删除三张后台运行表并完整还原到 0.7.0 数据库结构。"""
    op.drop_index(
        "ix_worker_leases_worker_id",
        table_name="worker_leases",
    )
    op.drop_index(
        "ix_worker_leases_status",
        table_name="worker_leases",
    )
    op.drop_index(
        "ix_worker_leases_expires_at",
        table_name="worker_leases",
    )
    op.drop_index(
        "ix_worker_leases_expiration",
        table_name="worker_leases",
    )
    op.drop_table("worker_leases")

    op.drop_index(
        "ix_scheduled_jobs_enabled",
        table_name="scheduled_jobs",
    )
    op.drop_table("scheduled_jobs")

    op.drop_index(
        "ix_background_jobs_thread_id",
        table_name="background_jobs",
    )
    op.drop_index(
        "ix_background_jobs_status",
        table_name="background_jobs",
    )
    op.drop_index(
        "ix_background_jobs_run_id",
        table_name="background_jobs",
    )
    op.drop_index(
        "ix_background_jobs_current_worker_id",
        table_name="background_jobs",
    )
    op.drop_index(
        "ix_background_jobs_claimable",
        table_name="background_jobs",
    )
    op.drop_index(
        "ix_background_jobs_available_at",
        table_name="background_jobs",
    )
    op.drop_table("background_jobs")
