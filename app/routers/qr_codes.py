"""
QR-коды для сущностей — интеграция мобильного приложения.

Эндпоинты:
  GET  /api/core/entities/{uid}/qr        QR-код для сущности (PNG)
  GET  /api/core/entities/{uid}/share-link  Share URL для сущности

QR-код содержит deep link в формате:
  zezar://entity/{uid}/{type}
  
Или HTTPS URL (fallback):
  https://zezar.example.com/entity/{uid}/{type}

При сканировании:
  - Если приложение установлено → открывается сущность в приложении
  - Если нет → открывается веб-версия на URL
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


def _generate_qr_data(entity_uid: str, entity_type: str) -> str:
    """Генерировать deep link для сущности."""
    settings = get_settings()
    
    # Primary: deep link scheme for mobile app
    deep_link = f"zezar://entity/{entity_uid}/{entity_type}"
    
    # Fallback: HTTPS URL for web
    base_url = settings.base_url or "https://zezar.example.com"
    web_url = f"{base_url}/entity/{entity_uid}/{entity_type}"
    
    # Return deep link (app will handle fallback)
    return deep_link


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
    
    # Generate QR code
    try:
        from qrcode import QRCode
        from qrcode.image.pil import PilImage
        
        qr = QRCode(
            version=1,
            error_correction=2,  # QRCode.ERROR_CORRECT_Q
            box_size=10,
            border=4,
        )
        qr.add_data(qr_data)
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
    settings = get_settings()
    base_url = settings.base_url or "https://zezar.example.com"
    
    return {
        "entity_uid": uid,
        "entity_type": entity_type,
        "entity_title": entity.title,
        "deep_link": f"zezar://entity/{uid}/{entity_type}",
        "web_url": f"{base_url}/entity/{uid}/{entity_type}",
        "qr_data": f"zezar://entity/{uid}/{entity_type}",
    }
