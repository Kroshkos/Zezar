"""таблица entity_links — произвольные связи между сущностями

Revision ID: m1i3f9b5c8d7
Revises: l0h2e8f4a7c6
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm1i3f9b5c8d7'
down_revision = 'l0h2e8f4a7c6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'entity_links',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('entity_a_id', sa.Integer(),
                  sa.ForeignKey('entities.id', ondelete='CASCADE'),
                  index=True, nullable=False),
        sa.Column('entity_b_id', sa.Integer(),
                  sa.ForeignKey('entities.id', ondelete='CASCADE'),
                  index=True, nullable=False),
        sa.Column('link_type', sa.String(50), nullable=False, server_default='related'),
        sa.Column('note', sa.String(500), nullable=True),
        sa.Column('stage_index', sa.Integer(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('entity_a_id', 'entity_b_id'),
    )


def downgrade() -> None:
    op.drop_table('entity_links')
