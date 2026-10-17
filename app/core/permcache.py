"""Инвалидация кэша прав (RBAC).

Права пользователя «закэшированы» в трёх местах, и любое изменение ролей/членств
должно сбрасывать ВСЕ три, иначе открытые страницы продолжают работать со
старыми правами до перезагрузки/перелогина:

  1. Session-scoped identity map AsyncSession — ORM-объекты (User.role,
     DepartmentMember.role_in_dept) загружаются один раз за запрос и повторно
     не читаются из БД (кэш уровня процесса/запроса);
  2. Подписанный session-cookie — содержит uid; при перевыпуске токена с новым
     полем ``acl_epoch`` старый cookie перестаёт проходить HMAC-проверку схемы
     epoch, и клиент получает актуальные права на следующем запросе;
  3. Фронтенд — страница держит серверный рендер (badge/selects) в DOM;
     обработчики обязаны перечитать состояние после мутации.

Эпоха ACL хранится в таблице ``acl_state`` (ключ ``acl_epoch``, модель
app.models.core.AclState) — это общий монотонный счётчик для всех
пользователей: любая мутация прав инкрементирует его, любое чтение
build_context сверяет epoch из cookie с текущим.
"""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

EPOCH_KEY = "acl_epoch"

# Локальный mirror последней известной эпохи: нужен, чтобы не трогать БД на
# каждом запросе чтения, когда ничего не менялось. Инкремент делает и setattr
# по этому dict — он же служит «event-флагом» для invalidate().
_local_epoch: dict[str, float] = {"value": 0.0, "at": 0.0}
_TTL = 5.0  # сек; дольше не держим — мультипроцессные воркеры обновятся сами


async def get_epoch(session: AsyncSession) -> int:
    """Текущая эпоха ACL (monotonic counter). 0 — никогда не инвалидировалось."""
    now = time.monotonic()
    if now - _local_epoch["at"] > _TTL:
        from app.models.core import AclState  # лениво: cycle-safe
        val = await session.scalar(
            select(AclState.value).where(AclState.key == EPOCH_KEY))
        try:
            _local_epoch["value"] = float(val or 0)
        except (TypeError, ValueError):
            _local_epoch["value"] = 0.0
        _local_epoch["at"] = now
    return int(_local_epoch["value"])


async def bump_epoch(session: AsyncSession) -> int:
    """Увеличить эпоху ACL. Вызывать при ЛЮБОМ изменении состава/ролей/шэров.

    Не выполняет commit: вызывающий код фиксирует транзакцию сам — epoch
    должен меняться атомарно вместе с самими правами."""
    from app.models.core import AclState  # лениво: cycle-safe
    new_val = await get_epoch(session) + 1
    row = await session.get(AclState, EPOCH_KEY)
    if row is None:
        session.add(AclState(key=EPOCH_KEY, value=str(new_val)))
    else:
        row.value = str(new_val)
    await session.flush()
    _local_epoch["value"] = float(new_val)
    _local_epoch["at"] = time.monotonic()
    return new_val


def invalidate_session(session: AsyncSession) -> None:
    """Сбросить session-scoped identity map: гарантируем, что повторное чтение
    User/DepartmentMember в рамках этого запроса вернёт свежие данные из БД."""
    session.expire_all()


def token_needs_refresh(token_data: dict | None, current_epoch: int) -> bool:
    """True — если cookie выпущен под более ранней эпохой ACL (или без epoch),
    т.е. права в токене/на клиенте потенциально устарели."""
    if token_data is None:
        return False
    tok_epoch = int(token_data.get("acl_epoch", 0) or 0)
    return tok_epoch != current_epoch
