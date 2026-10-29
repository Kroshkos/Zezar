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


def _has_stages() -> bool:
    """Колонка stages уже есть в doc_payloads?

    Легаси-БД, созданные старым create_all и наштампованные на head, могут
    содержать doc_payloads.stages (колонка добавлена в модель) — тогда
    повторный ADD COLUMN падает с "duplicate column name".
    """
    from alembic import op as _op
    conn = _op.get_bind()
    try:
        rows = conn.exec_driver_sql("PRAGMA table_info(doc_payloads)").fetchall()
    except Exception:  # таблица ещё не создана
        return False
    return any(r[1] == 'stages' for r in rows)


def upgrade() -> None:
    if _has_stages():
        # схема уже самовосстановлена (create_all/_sync_missing_columns) —
        # достаточно заполнять NULL для истории
        op.execute("UPDATE doc_payloads SET stages = '[]' WHERE stages IS NULL")
        return
    op.add_column(
        'doc_payloads',
        sa.Column('stages', sa.JSON(), nullable=True),
    )
    # исторические строки без маршрута -> пустой список (модель ожидает list)
    # ('[]'::json — синтаксис PostgreSQL, не поддерживается SQLite)
    op.execute("UPDATE doc_payloads SET stages = '[]' WHERE stages IS NULL")


def downgrade() -> None:
    if not _has_stages():
        return
    with op.batch_alter_table('doc_payloads') as batch_op:
        batch_op.drop_column('stages')
