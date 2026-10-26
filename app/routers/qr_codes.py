"""
QR-коды для сущностей — интеграция мобильного приложения.

Эндпоинты:
  GET  /api/core/entities/{uid}/qr        QR-код для сущности (PNG)
  GET  /api/core/entities/{uid}/share-link  Share URL для сущности

QR-код содержит универсальную HTTPS-ссылку шлюза:
  {PUBLIC_BASE_URL}/open/e/{uid}

При сканировании камерой (см. app/routers/entity_open.py):
  - Если приложение Zezar установлено → сущность открывается в приложении
  - Если нет → открывается веб-версия карточки на сайте
"""
from __future__ import annotations

import io
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.deps import session_dep
from app.models.core import Entity, EntityKind, User

logger = logging.getLogger("caesar.qr")

router = APIRouter()


def _get_entity_kind_type(entity: Entity) -> str:
    """Получить тип сущности из kind enum."""
    kind_map = {
        EntityKind.TASK: "task",
        EntityKind.DOC: "doc",
        EntityKind.SOFT: "soft",
        EntityKind.PROJECT: "project",
        EntityKind.GRANT: "grant",
        EntityKind.SCHOL: "scholarship",
        EntityKind.PRIORITY: "priority",
        EntityKind.MEETING: "meeting",
    }
    return kind_map.get(entity.kind, str(entity.kind))


def build_deep_link(uid: str) -> str:
    """Deep link мобильного приложения из DEEP_LINK_BASE + '/entity/{uid}'.

    Нормализует конфиг так, чтобы на выходе всегда была корректная схема:
      'zezar' / 'zezar:' / 'zezar:/' / 'zezar://'  -> zezar://entity/{uid}
      'https://open.zezar.app[/...]'               -> https://open.zezar.app/entity/{uid}
    (битые 'zezar:/entity/…' и 'zezar:///entity/…' ОС не открывают).
    """
    base = (get_settings().deep_link_base or "zezar://").strip()
    if ":" in base.split("/", 1)[0]:          # есть схема ('zezar:' / 'https:')
        scheme, rest = base.split(":", 1)
        rest = rest.strip("/")                 # host/path после схемы
        return f"{scheme}://{rest}/entity/{uid}" if rest else f"{scheme}://entity/{uid}"
    base = base.rstrip("/")                    # без схемы: трактует как host/path или схему
    if "/" in base:
        return f"{base}/entity/{uid}"
    return f"{base}://entity/{uid}"


def _gateway_url(uid: str) -> str:
    """Универсальная HTTPS-ссылка шлюза: открывает приложение, если оно
    установлено, иначе — карточку на сайте. Именно она кладётся в QR-код:
    встроенные сканеры камер (iOS/Android) надёжно открывают HTTPS-ссылки,
    а custom-scheme прямо из камеры часто блокируется."""
    base = (get_settings().public_base_url or "").rstrip("/")
    return f"{base}/open/e/{uid}"


def _generate_qr_data(entity_uid: str, entity_type: str) -> str:
    """Данные для QR-кода сущности (HTTPS-шлюз app-or-web).

    Ссылка подбирается так, чтобы код оставался компактным (QR версии 2):
    /open/e/{uid}, а при длинном домене/uid — короткий алиас /o/{uid}
    (см. app.routers.entity_open.gateway_url_from_base)."""
    from app.routers.entity_open import gateway_url_from_base
    return gateway_url_from_base(get_settings().public_base_url or "", entity_uid)


@router.get("/api/core/entities/{uid}/qr")
async def get_entity_qr(
    uid: str,
    user: User = Depends(lambda: None),  # QR can be public
    session: AsyncSession = Depends(session_dep),
):
    """Получить QR-код для сущности в формате PNG.
    
    QR-код содержит deep link, который при сканировании:
    - Открывает сущность в мобильном приложении (если установлено)
    - Или редиректит на веб-версию (если приложения нет)
    """
    # Find entity
    result = await session.execute(
        select(Entity).where(Entity.uid == uid)
    )
    entity = result.scalar_one_or_none()
    
    if entity is None:
        raise HTTPException(
            status_code=404,
            detail="Сущность не найдена",
        )
    
    # Generate deep link
    entity_type = _get_entity_kind_type(entity)
    qr_data = _generate_qr_data(entity.uid, entity_type)
    
    # Генерируем компактный читаемый QR: фиксированная версия 2 (25x25),
    # средний уровень коррекции, крупный модуль — такие коды надёжно
    # распознаются камерой телефона с печатной карточки.
    try:
        from qrcode import QRCode
        from qrcode.constants import ERROR_CORRECT_M

        qr = QRCode(
            version=2,
            error_correction=ERROR_CORRECT_M,
            box_size=12,
            border=4,
        )
        qr.add_data(qr_data)
        # fit=True: версия 2 как минимум; если ссылка не влезает — код
        # автоматически станет больше, но останется читаемым (не упадёт).
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white").convert('RGB')
        
        # Add logo/text overlay
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(img)
        
        # Add small text at bottom
        try:
            font = ImageFont.truetype("arial.ttf", 20)
        except:
            font = ImageFont.load_default()
        
        text = "Zezar"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_x = (img.width - text_width) // 2
        text_y = img.height - 40
        draw.text((text_x, text_y), text, fill=(37, 99, 235), font=font)
        
        # Save to bytes
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        
        return StreamingResponse(
            buffer,
            media_type="image/png",
            headers={
                "Content-Disposition": f'inline; filename="qr-{uid}.png"',
            },
        )
        
    except ImportError:
        # qrcode library not installed, return simple response
        raise HTTPException(
            status_code=503,
            detail="QR-генерация недоступна (установите qrcode Pillow)",
        )


@router.get("/api/core/entities/{uid}/share-link")
async def get_entity_share_link(
    uid: str,
    user: User = Depends(lambda: None),
    session: AsyncSession = Depends(session_dep),
):
    """Получить share URL для сущности.
    
    Возвращает JSON с deep link и web URL.
    """
    result = await session.execute(
        select(Entity).where(Entity.uid == uid)
    )
    entity = result.scalar_one_or_none()
    
    if entity is None:
        raise HTTPException(
            status_code=404,
            detail="Сущность не найдена",
        )
    
    entity_type = _get_entity_kind_type(entity)
    gateway = _generate_qr_data(uid, entity_type)

    return {
        "entity_uid": uid,
        "entity_type": entity_type,
        "entity_title": entity.title,
        # Универсальная ссылка для QR и шаринга: приложение или сайт
        "share_url": gateway,
        "qr_data": gateway,
        "deep_link": build_deep_link(uid),
        "web_url": gateway,
    }
