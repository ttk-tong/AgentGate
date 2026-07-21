"""stage5 task and schedule

Revision ID: 0e39e5a76802
Revises: eb775c0e86ec
Create Date: 2026-07-20 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0e39e5a76802'
down_revision: Union[str, None] = 'eb775c0e86ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'task',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=True),
        sa.Column('type', sa.Text(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('max_attempts', sa.Integer(), nullable=False),
        sa.Column('next_run_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('locked_by', sa.Text(), nullable=True),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('idempotency_key', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_task_dispatch', 'task', ['status', 'next_run_at'], unique=False)
    op.create_index(
        'ux_task_idem',
        'task',
        ['idempotency_key'],
        unique=True,
        postgresql_where=sa.text('idempotency_key IS NOT NULL'),
    )
    op.create_table(
        'schedule',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=True),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('cron', sa.Text(), nullable=False),
        sa.Column('task_type', sa.Text(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('last_fired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_fire_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_schedule_due', 'schedule', ['enabled', 'next_fire_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_schedule_due', table_name='schedule')
    op.drop_table('schedule')
    op.drop_index('ux_task_idem', table_name='task')
    op.drop_index('ix_task_dispatch', table_name='task')
    op.drop_table('task')
