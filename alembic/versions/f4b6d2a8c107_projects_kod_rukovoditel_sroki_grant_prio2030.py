"""Проекты: код, руководитель, даты начала/окончания, связанный грант, показатель Приоритет-2030

По образцу sipm_lite_v3.html в форме проекта появляются:
  - code (код проекта, напр. «П-01»);
  - leader_user_id (руководитель проекта из участников подразделения, FK users.id);
  - start_date / end_date (дата начала / окончания);
  - grant_entity_uid (связанный грант — uid сущности kind=grant);
  - priority_indicator_uid (показатель Приоритет-2030 — uid сущности kind=priority).
Поле budget остаётся в БД для исторических данных, но удалено из формы/UI по ТЗ.

Все изменения идемпотентны (проверка через sa.inspect).

Revision ID: f4b6d2a8c107
Revises: e3a5c7d91f02
Create Date: 2026-10-17 21:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'f4b6d2a8c107'
down_revision: Union[str, None] = 'e3a5c7d91f02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ADD_COLUMNS = [
    ("code", sa.String(length=64)),
    ("start_date", sa.String(length=32)),
    ("end_date", sa.String(length=32)),
]
_UID_COLUMNS = ["grant_entity_uid", "priority_indicator_uid"]


def _insp():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    insp = _insp()
    if "project_payloads" not in insp.get_table_names():
        return  # чистая установка через create_all — колонки уже в модели
    cols = {c["name"] for c in insp.get_columns("project_payloads")}
    for name, typ in _ADD_COLUMNS:
        if name not in cols:
            op.add_column("project_payloads", sa.Column(name, typ, nullable=True))
    # leader_user_id: INTEGER + FK users.id (ON DELETE SET NULL).
    # ВАЖНО: в SQLite ALTER TABLE ADD CONSTRAINT не поддерживается, поэтому FK
    # добавляется через batch-режим (copy-and-move) — ровно как исправленная
    # ревизия a1b7c3d94e51. Plain op.create_foreign_key падает с
    # NotImplementedError либо тихо игнорируется и теряет данные таблицы.
    has_fk = lambda: any(
        (fk["name"] or "") == "fk_project_payloads_leader_user_id"
        or (fk["referred_table"] == "users" and "leader_user_id" in fk["constrained_columns"])
        for fk in _insp().get_foreign_keys("project_payloads")
    )
    if "leader_user_id" not in cols:
        op.add_column("project_payloads", sa.Column(
            "leader_user_id", sa.Integer(), nullable=True))
    if not has_fk():
        with op.batch_alter_table(
            "project_payloads",
            naming_convention={"fk": "fk_project_payloads_leader_user_id"},
        ) as batch:
            batch.create_foreign_key(
                "fk_project_payloads_leader_user_id", "users",
                ["leader_user_id"], ["id"], ondelete="SET NULL")
    for name in _UID_COLUMNS:
        if name not in cols:
            op.add_column("project_payloads", sa.Column(
                name, sa.String(length=36), nullable=True))
    insp = _insp()
    idx = {i["name"] for i in insp.get_indexes("project_payloads")}
    for name in _UID_COLUMNS:
        ix = f"ix_project_payloads_{name}"
        if ix not in idx:
            op.create_index(ix, "project_payloads", [name])


def downgrade() -> None:
    insp = _insp()
    if "project_payloads" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("project_payloads")}
    fk_names = {fk["name"] or "" for fk in insp.get_foreign_keys("project_payloads")}
    if "fk_project_payloads_leader_user_id" in fk_names:
        with op.batch_alter_table("project_payloads") as batch:
            batch.drop_constraint("fk_project_payloads_leader_user_id",
                                  type_="foreignkey")
    for name in [c for c, _ in _ADD_COLUMNS] + _UID_COLUMNS + ["leader_user_id"]:
        if name in cols:
            with op.batch_alter_table("project_payloads") as batch:
                batch.drop_column(name)
