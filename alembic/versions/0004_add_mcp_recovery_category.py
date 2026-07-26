from __future__ import annotations

from alembic import op

"""本迁移扩展错误恢复类别约束，使 Worktree 与邮件 MCP 错误可被安全持久化。"""


# 当前 0.8.0 邮件 MCP 恢复类别迁移版本标识。
revision = "0004_mcp_recovery_category"

# 当前迁移基于 0.7.2 后台队列、计划和 Worker 租约版本。
down_revision = "0003_background_runtime_tables"

# 当前迁移不属于并行分支。
branch_labels = None

# 当前迁移没有额外依赖。
depends_on = None


# 0.8.0 允许写入 error_recovery_records 的完整错误类别约束。
UPGRADED_CATEGORY_CONSTRAINT = (
    "category IN ('filesystem', 'parse', 'comparison', 'evidence', "
    "'llm', 'validation', 'protocol', 'prompt', 'hook', 'memory', "
    "'skill', 'context', 'database', 'checkpoint', 'worktree', 'mcp', "
    "'timeout', 'unknown')"
)

# 0.7.0 原始错误恢复表使用的类别约束。
LEGACY_CATEGORY_CONSTRAINT = (
    "category IN ('filesystem', 'parse', 'comparison', 'evidence', "
    "'llm', 'validation', 'protocol', 'prompt', 'hook', 'memory', "
    "'skill', 'context', 'database', 'checkpoint', 'timeout', 'unknown')"
)


def _replace_category_constraint(expression: str) -> None:
    """通过 SQLite batch 重建替换错误类别 CheckConstraint。

    Args:
        expression: 迁移目标版本允许的完整 category SQL 表达式。
    """
    with op.batch_alter_table(
        "error_recovery_records",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_error_recovery_records_category_allowed",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_error_recovery_records_category_allowed",
            expression,
        )


def upgrade() -> None:
    """允许恢复记录持久化 0.7.2 Worktree 和 0.8.0 MCP 错误类别。"""
    _replace_category_constraint(UPGRADED_CATEGORY_CONSTRAINT)


def downgrade() -> None:
    """把错误类别约束恢复为 0.7.0 集合。"""
    _replace_category_constraint(LEGACY_CATEGORY_CONSTRAINT)
