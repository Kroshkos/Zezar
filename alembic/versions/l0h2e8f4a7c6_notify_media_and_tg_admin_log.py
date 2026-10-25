"""media-вложения рассылок + таблица tg_admin_log (журнал действий TG-бота)

Revision ID: l0h2e8f4a7c6
Revises: k9g1d7e3f6b5
Create Date: 2026-10-25
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'l0h2e8f4a7c6'
down_revision = 'k9g1d7e3f6b5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # media_json: список вложений рассылки [{file_id, type, caption}] —
    # Telegram file_id, загруженный через uploadMediaFile (бот или админ-чат)
    op.add_column('notifications',
                  sa.Column('media_json', sa.JSON(), nullable=True))

    op.create_table(
        'tg_admin_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='SET NULL'),
                  index=True, nullable=True),
        sa.Column('action', sa.String(50), nullable=False),
        sa.Column('details', sa.Text(), default=''),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('tg_admin_log')
    op.drop_column('notifications', 'media_json')
