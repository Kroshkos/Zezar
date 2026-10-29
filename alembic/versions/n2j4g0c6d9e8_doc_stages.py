"""doc_payloads.stages — маршрут (workflow) документов

Revision ID: n2j4g0c6d9e8
Revises: m1i3f9b5c8d7
Create Date: 2026-10-29
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'n2j4g0c6d9e8'
down_revision = 'm1i3f9b5c8d7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'doc_payloads',
        sa.Column('stages', sa.JSON(), nullable=True),
    )
    # исторические строки без маршрута -> пустой список (модель ожидает list)
    op.execute("UPDATE doc_payloads SET stages = '[]'::json WHERE stages IS NULL")
    op.alter_column('doc_payloads', 'stages', nullable=False,
                    server_default=sa.text("'[]'"))


def downgrade() -> None:
    op.drop_column('doc_payloads', 'stages')
