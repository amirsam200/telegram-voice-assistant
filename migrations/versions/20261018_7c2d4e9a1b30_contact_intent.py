"""contact intent

Revision ID: 7c2d4e9a1b30
Revises: 54218d572d1c
Create Date: 2026-10-18 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = '7c2d4e9a1b30'
down_revision: Union[str, None] = '54218d572d1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Runs that add or delete a contact; ADD VALUE cannot be undone, so downgrade leaves it in place.
    op.execute("ALTER TYPE agent_intent ADD VALUE IF NOT EXISTS 'contact'")


def downgrade() -> None:
    pass
