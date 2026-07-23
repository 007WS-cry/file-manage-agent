from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

"""${message}"""


# 当前迁移版本的唯一标识。
revision = ${repr(up_revision)}

# 当前迁移直接依赖的上一版本标识。
down_revision = ${repr(down_revision)}

# 可选分支标签；普通线性迁移保持为 None。
branch_labels = ${repr(branch_labels)}

# 可选额外依赖；普通线性迁移保持为 None。
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    """把应用数据库升级到当前迁移版本。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """把应用数据库从当前迁移版本回退到上一版本。"""
    ${downgrades if downgrades else "pass"}
