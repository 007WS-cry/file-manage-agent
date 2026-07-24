from __future__ import annotations

import sqlalchemy as sa

from alembic import op

"""本迁移新增错误恢复与节点幂等执行表，并允许治理运行进入 recovering 状态。"""


# 当前 0.6.2 恢复与幂等持久化迁移版本标识。
revision = "0002_error_recovery_tables"

# 当前迁移基于 0.5.1 创建五张应用表的首个版本。
down_revision = "0001_application_tables"

# 当前迁移不属于并行分支。
branch_labels = None

# 当前迁移没有额外依赖。
depends_on = None


def upgrade() -> None:
    """新增两张恢复表，并扩展治理运行状态约束。"""
    with op.batch_alter_table("governance_runs") as batch_op:
        batch_op.drop_constraint(
            "ck_governance_runs_status_allowed",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_governance_runs_status_allowed",
            "status IN ('created', 'queued', 'running', 'recovering', "
            "'waiting_human', 'completed', 'partial', 'failed')",
        )

    op.create_table(
        "node_execution_records",
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_execution_id", sa.String(length=192), nullable=True),
        sa.Column("task_id", sa.String(length=160), nullable=True),
        sa.Column("stage", sa.String(length=128), nullable=False),
        sa.Column("node_name", sa.String(length=128), nullable=False),
        sa.Column("input_digest", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("state_update_ref", sa.Text(), nullable=True),
        sa.Column("result_refs", sa.JSON(), nullable=False),
        sa.Column("result_digest", sa.String(length=128), nullable=True),
        sa.Column("last_error_id", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_node_execution_records_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'reused')",
            name="ck_node_execution_records_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["governance_runs.run_id"],
            name="fk_node_execution_records_run_id_governance_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "idempotency_key",
            name="pk_node_execution_records",
        ),
    )
    op.create_index(
        "ix_node_execution_records_last_error_id",
        "node_execution_records",
        ["last_error_id"],
        unique=False,
    )
    op.create_index(
        "ix_node_execution_records_node_name",
        "node_execution_records",
        ["node_name"],
        unique=False,
    )
    op.create_index(
        "ix_node_execution_records_run_id",
        "node_execution_records",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "ix_node_execution_records_task_execution_id",
        "node_execution_records",
        ["task_execution_id"],
        unique=False,
    )
    op.create_index(
        "ix_node_execution_records_task_id",
        "node_execution_records",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "ix_node_execution_records_run_status_updated",
        "node_execution_records",
        ["run_id", "status", "updated_at"],
        unique=False,
    )

    op.create_table(
        "error_recovery_records",
        sa.Column("record_id", sa.String(length=320), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("error_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=160), nullable=True),
        sa.Column("node_execution_id", sa.String(length=128), nullable=True),
        sa.Column("stage", sa.String(length=128), nullable=False),
        sa.Column("node_name", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("exception_type", sa.String(length=128), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("related_file_id", sa.String(length=128), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("fallback", sa.String(length=32), nullable=True),
        sa.Column("requires_human", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("fatal", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('none', 'retry', 'reuse_result', 'skip_file', "
            "'fallback', 'continue_partial', 'wait_human', 'abort')",
            name="ck_error_recovery_records_action_allowed",
        ),
        sa.CheckConstraint(
            "category IN ('filesystem', 'parse', 'comparison', 'evidence', "
            "'llm', 'validation', 'protocol', 'prompt', 'hook', 'memory', "
            "'skill', 'context', 'database', 'checkpoint', 'timeout', 'unknown')",
            name="ck_error_recovery_records_category_allowed",
        ),
        sa.CheckConstraint(
            "fallback IS NULL OR fallback IN ('skip_file', 'coordinator', "
            "'no_memory', 'default_skill', 'keep_context', 'partial_result')",
            name="ck_error_recovery_records_fallback_allowed",
        ),
        sa.CheckConstraint(
            "retry_count >= 0 AND max_retries >= 0 AND retry_count <= max_retries",
            name="ck_error_recovery_records_retry_counts_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'retrying', 'fallback_applied', "
            "'waiting_human', 'recovered', 'failed')",
            name="ck_error_recovery_records_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["node_execution_id"],
            ["node_execution_records.idempotency_key"],
            name=("fk_error_recovery_records_node_execution_id_node_execution_records"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["governance_runs.run_id"],
            name="fk_error_recovery_records_run_id_governance_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "record_id",
            name="pk_error_recovery_records",
        ),
        sa.UniqueConstraint(
            "run_id",
            "error_id",
            name="uq_error_recovery_records_run_error",
        ),
    )
    op.create_index(
        "ix_error_recovery_records_category",
        "error_recovery_records",
        ["category"],
        unique=False,
    )
    op.create_index(
        "ix_error_recovery_records_error_id",
        "error_recovery_records",
        ["error_id"],
        unique=False,
    )
    op.create_index(
        "ix_error_recovery_records_node_execution_id",
        "error_recovery_records",
        ["node_execution_id"],
        unique=False,
    )
    op.create_index(
        "ix_error_recovery_records_run_id",
        "error_recovery_records",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "ix_error_recovery_records_task_id",
        "error_recovery_records",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "ix_error_recovery_records_run_status_updated",
        "error_recovery_records",
        ["run_id", "status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """删除两张恢复表，并还原治理运行状态约束。"""
    op.drop_index(
        "ix_error_recovery_records_run_status_updated",
        table_name="error_recovery_records",
    )
    op.drop_index(
        "ix_error_recovery_records_task_id",
        table_name="error_recovery_records",
    )
    op.drop_index(
        "ix_error_recovery_records_run_id",
        table_name="error_recovery_records",
    )
    op.drop_index(
        "ix_error_recovery_records_node_execution_id",
        table_name="error_recovery_records",
    )
    op.drop_index(
        "ix_error_recovery_records_error_id",
        table_name="error_recovery_records",
    )
    op.drop_index(
        "ix_error_recovery_records_category",
        table_name="error_recovery_records",
    )
    op.drop_table("error_recovery_records")

    op.drop_index(
        "ix_node_execution_records_run_status_updated",
        table_name="node_execution_records",
    )
    op.drop_index(
        "ix_node_execution_records_task_id",
        table_name="node_execution_records",
    )
    op.drop_index(
        "ix_node_execution_records_task_execution_id",
        table_name="node_execution_records",
    )
    op.drop_index(
        "ix_node_execution_records_run_id",
        table_name="node_execution_records",
    )
    op.drop_index(
        "ix_node_execution_records_node_name",
        table_name="node_execution_records",
    )
    op.drop_index(
        "ix_node_execution_records_last_error_id",
        table_name="node_execution_records",
    )
    op.drop_table("node_execution_records")

    with op.batch_alter_table("governance_runs") as batch_op:
        batch_op.drop_constraint(
            "ck_governance_runs_status_allowed",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_governance_runs_status_allowed",
            "status IN ('created', 'queued', 'running', 'waiting_human', "
            "'completed', 'partial', 'failed')",
        )
