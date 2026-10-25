"""Android App Links: верификация домена для прямого открытия приложения.

Без этого файла Android (Chrome/камера) НЕ перехватывает HTTPS-ссылки вида
https://host/open/e/{uid} приложением — они всегда уходят в браузер, и
QR-шлюз работает только через промежуточную страницу с custom scheme
(которую современные браузеры тоже часто блокируют). С assetlinks.json и
intentFiltersAuto в expo конфигурации система сама решает «приложение или
сайт» мгновенно, без таймаутов.

Раздаётся по пути /.well-known/assetlinks.json (требование Google:
application/.well-known/assetlinks.json на корне домена).
"""
from __future__ import annotations

import base64
import binascii
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

router = APIRouter()

# SHA-256 сертификат debug-ключа EAS/Expo (constant, публичный):
DEBUG_FINGERPRINT_B64 = "8JhravYgAac2embLSlgZaWlPJGKTfSCw/b7Kb0YmTGY="


def _release_fingerprints() -> list[str]:
    """Отпечатки релизного keystore из env (base64 SHA-256 или hex, через запятую)."""
    raw = os.getenv("ANDROID_CERT_FINGERPRINTS", "").strip()
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip().replace(":", "")
        if not part:
            continue
        try:  # hex-форма из keytool
            part = base64.b64encode(bytes.fromhex(part)).decode()
        except ValueError:
            pass  # уже base64
        out.append(part)
    return out


@router.get("/.well-known/assetlinks.json", include_in_schema=False)
async def asset_links():
    fps = [DEBUG_FINGERPRINT_B64] + _release_fingerprints()
    payload = [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "app.zezar.mobile",
                "sha256_cert_fingerprints": fps,
            },
        }
    ]
    return JSONResponse(payload, headers={
        "Cache-Control": "public, max-age=3600",
        "Content-Type": "application/assetlinks+json; charset=utf-8",
    })
