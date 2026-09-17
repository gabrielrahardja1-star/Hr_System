"""collapse every shift onto the single KERJA shift

The site does not run fixed shifts, so per-person shift assignment was
removing information rather than adding it. One 07:30-to-07:30 window covers
every pattern worked here, and hours have always come from the punches
themselves.

Revision ID: d4e8a1c39f77
Revises: c3a5e81b6d42
Create Date: 2026-09-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4e8a1c39f77'
down_revision: Union[str, Sequence[str], None] = 'c3a5e81b6d42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Includes NULLs: with one shift there is nothing left to decide, so a
    # worker registered before anyone knew their pattern now computes normally.
    for table in ("employees", "day_records"):
        op.execute(sa.text(f"UPDATE {table} SET shift_key = 'KERJA'"))


def downgrade() -> None:
    # The old per-person shift is gone and cannot be recovered from the data.
    # S1 is the least surprising landing place; reassign by hand afterwards.
    for table in ("employees", "day_records"):
        op.execute(
            sa.text(f"UPDATE {table} SET shift_key = 'S1' WHERE shift_key = 'KERJA'")
        )
