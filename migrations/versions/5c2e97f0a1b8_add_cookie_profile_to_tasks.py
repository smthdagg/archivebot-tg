"""add cookie_profile to tasks

Revision ID: 5c2e97f0a1b8
Revises: 51f9c3d7e2a4
Create Date: 2026-09-02 00:30:00.000000


"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c2e97f0a1b8"
down_revision: str | None = "51f9c3d7e2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("cookie_profile", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(op.f("ix_tasks_cookie_profile"), ["cookie_profile"])


def downgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_tasks_cookie_profile"))
        batch_op.drop_column("cookie_profile")
