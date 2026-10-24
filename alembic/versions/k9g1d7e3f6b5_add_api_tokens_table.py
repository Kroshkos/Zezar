"""добавить таблицу api_tokens для мобильного приложения

Revision ID: k9g1d7e3f6b5
Revises: j8f0c6d2e5a4
Create Date: 2026-10-24
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'k9g1d7e3f6b5'
down_revision = 'j8f0c6d2e5a4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создать таблицу api_tokens для мобильного приложения."""
    op.create_table(
        'api_tokens',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), index=True, nullable=False),
        sa.Column('token_hash', sa.String(64), unique=True, index=True, nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Удалить таблицу api_tokens."""
    op.drop_table('api_tokens')
