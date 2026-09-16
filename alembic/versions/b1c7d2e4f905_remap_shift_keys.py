"""remap shift keys to the three real shifts

config/shifts.yaml now declares S1 (08:00-15:00), S2 (16:00-00:00) and
S3 (23:00-07:00) in place of A/B/OFFICE. ShiftConfig.get() raises on an
unknown key, so any row left on an old key would break recompute.

Revision ID: b1c7d2e4f905
Revises: ead906254ac1
Create Date: 2026-09-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b1c7d2e4f905'
down_revision: Union[str, Sequence[str], None] = 'ead906254ac1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# A (06:00-14:30) and OFFICE (08:00-17:00) both become the morning shift;
# B (18:00-02:00) becomes the evening shift. Nobody was on a 23:00 shift
# before, so nothing maps to S3 — night crews are assigned by hand.
FORWARD = {"A": "S1", "OFFICE": "S1", "B": "S2"}
BACKWARD = {"S1": "A", "S2": "B", "S3": "B"}


def _remap(mapping: dict[str, str]) -> None:
    for table in ("employees", "day_records"):
        for old, new in mapping.items():
            op.execute(
                sa.text(
                    f"UPDATE {table} SET shift_key = :new WHERE shift_key = :old"
                ).bindparams(new=new, old=old)
            )


def upgrade() -> None:
    _remap(FORWARD)


def downgrade() -> None:
    _remap(BACKWARD)
