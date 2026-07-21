"""stage6 memory_item

Revision ID: a1b2c3d4e5f6
Revises: 0e39e5a76802
Create Date: 2026-07-21 11:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '0e39e5a76802'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'memory_item',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=True),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('scope_key', sa.Text(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('importance', sa.Float(), nullable=False),
        sa.Column('source_event_id', sa.UUID(), nullable=True),
        sa.Column('use_count', sa.Integer(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_memory_scope', 'memory_item', ['scope', 'scope_key'], unique=False)
    op.create_index('ix_memory_tenant', 'memory_item', ['tenant_id'], unique=False)
    op.create_index('ix_memory_dedup', 'memory_item', ['scope', 'scope_key', 'kind'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_memory_dedup', table_name='memory_item')
    op.drop_index('ix_memory_tenant', table_name='memory_item')
    op.drop_index('ix_memory_scope', table_name='memory_item')
    op.drop_table('memory_item')
