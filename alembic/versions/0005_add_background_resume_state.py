from __future__ import annotations

import sqlalchemy as sa

from alembic import op

"""本迁移为后台任务新增中断快照、幂等恢复状态及独立恢复计数。"""


# 当前 0.8.1 后台人工恢复状态迁移版本标识。
revision = "0005_background_resume_state"

# 当前迁移基于 0.8.0 邮件 MCP 错误类别版本。
down_revision = "0004_mcp_recovery_category"

# 当前迁移不属于并行分支。
branch_labels = None

# 当前迁移没有额外依赖。
depends_on = None


# 0.8.1 后台任务允许进入的完整状态约束。
UPGRADED_STATUS_CONSTRAINT = (
    "status IN ('queued', 'resume_queued', 'leased', 'running', "
    "'waiting_human', 'completed', 'partial', 'failed')"
)

# 0.8.0 后台任务原有状态约束。
LEGACY_STATUS_CONSTRAINT = (
    "status IN ('queued', 'leased', 'running', 'waiting_human', 'completed', 'partial', 'failed')"
)

# 0.8.1 异常尝试次数与人工恢复次数的完整约束。
UPGRADED_COUNT_CONSTRAINT = (
    "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts "
    "AND resume_count >= 0"
)

# 0.8.0 只包含异常尝试次数的原有约束。
LEGACY_COUNT_CONSTRAINT = (
    "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts"
)


def upgrade() -> None:
    """新增恢复列，并允许任务进入独立的恢复排队状态。"""
    with op.batch_alter_table(
        "background_jobs",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_background_jobs_status_allowed",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_background_jobs_attempt_counts_valid",
            type_="check",
        )
        batch_op.add_column(
            sa.Column(
                "resume_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("pending_interrupt", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("resume_state", sa.JSON(), nullable=True))
        batch_op.create_check_constraint(
            "ck_background_jobs_status_allowed",
            UPGRADED_STATUS_CONSTRAINT,
        )
        batch_op.create_check_constraint(
            "ck_background_jobs_attempt_counts_valid",
            UPGRADED_COUNT_CONSTRAINT,
        )


def downgrade() -> None:
    """删除恢复列，并还原 0.8.0 后台任务状态与计数约束。"""
    with op.batch_alter_table(
        "background_jobs",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_background_jobs_status_allowed",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_background_jobs_attempt_counts_valid",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_background_jobs_status_allowed",
            LEGACY_STATUS_CONSTRAINT,
        )
        batch_op.create_check_constraint(
            "ck_background_jobs_attempt_counts_valid",
            LEGACY_COUNT_CONSTRAINT,
        )
        batch_op.drop_column("resume_state")
        batch_op.drop_column("pending_interrupt")
        batch_op.drop_column("resume_count")
