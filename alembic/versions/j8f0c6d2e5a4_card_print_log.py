"""card_print_log: журнал печати печатных карточек

Revision ID: j8f0c6d2e5a4
Revises: i7e9b5c1d4f3
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'j8f0c6d2e5a4'
down_revision: Union[str, None] = 'i7e9b5c1d4f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'card_print_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('printed_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_card_print_log_entity_id', 'card_print_log', ['entity_id'])
    op.create_index('ix_card_print_log_printed_at', 'card_print_log', ['printed_at'])


def downgrade() -> None:
    op.drop_index('ix_card_print_log_printed_at', table_name='card_print_log')
    op.drop_index('ix_card_print_log_entity_id', table_name='card_print_log')
    op.drop_table('card_print_log')
