"""allow an employee to have no shift yet

Workers get registered before anyone has decided which shift they work.
Attendance is simply not computed for them until a shift is set.

Revision ID: c3a5e81b6d42
Revises: b1c7d2e4f905
Create Date: 2026-09-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3a5e81b6d42'
down_revision: Union[str, Sequence[str], None] = 'b1c7d2e4f905'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "employees", "shift_key",
        existing_type=sa.String(length=16),
        nullable=True,
    )


def downgrade() -> None:
    unassigned = op.get_bind().execute(
        sa.text("SELECT count(*) FROM employees WHERE shift_key IS NULL")
    ).scalar_one()
    if unassigned:
        # Inventing a shift would silently mis-attribute their attendance, and
        # deleting them would lose real people. A human picks.
        raise RuntimeError(
            f"{unassigned} employee(s) have no shift. Assign one to each before "
            "downgrading, or this column cannot go back to NOT NULL."
        )
    op.alter_column(
        "employees", "shift_key",
        existing_type=sa.String(length=16),
        nullable=False,
    )
