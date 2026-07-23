"""enforce per-session event sequence uniqueness

Revision ID: c4d5e6f7a8b9
Revises: a1b2c3d4e5f6
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.create_unique_constraint("uq_session_event_session_seq", "session_event", ["session_id", "seq"])

def downgrade() -> None:
    op.drop_constraint("uq_session_event_session_seq", "session_event", type_="unique")
