"""Контент сайта документации/новостей («/docs»): Markdown из репозитория.

Схема работы (пожелание владельца проекта):
  * news/<файл>.md   — новости; подгружаются на сайт в раздел «Новости»;
  * docs/<файл>.md   — документация; подгружаются в раздел «Документация».

Формат файла: опциональный YAML-фронтматтер между строками `---` в начале,
затем основной текст в Markdown. Поддерживаемые поля фронтматтера (все необязательные):

  title: Заголовок статьи      (по умолчанию — имя файла без расширения)
  date: 2026-10-25             (ISO; по умолчанию — mtime файла)
  order: 10                    (целое; сортировка документации, меньше = выше)
  tag: Release                 (метка новости)

Без жёсткой зависимости от PyYAML: разбор простого key: value сделан вручную.
Раздел «docs» в этом модуле — папка репозитория (не путать со static/docs —
это файлы самого сайта-одностраничника).

Markdown → HTML выполняется библиотекой `markdown` (fenced_code + tables +
nl2br); если библиотека недоступна, отдаём текст как-is в <pre>, а сайт
подхватывает markdown-it из content.json (поле markdown).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import markdown as _md  # type: ignore
except ImportError:  # pragma: no cover — деградация до сырого markdown
    _md = None

REPO_ROOT = Path(__file__).resolve().parent.parent
NEWS_DIR = REPO_ROOT / "news"
DOCS_DIR = REPO_ROOT / "docs"

# id статьи = имя файла без .md (только латиница/цифры/-/_), чтобы ссылки в
# hash-маршрутизации сайта (#docs/intro, #news/2026-10-release) были безопасны.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Возвращает (meta, body). Фронтматтер — key: value между `---`."""
    meta: dict[str, str] = {}
    if text.startswith("---"):
        parts = text.split("\n")
        if parts[0].strip() == "---":
            for i in range(1, len(parts)):
                line = parts[i]
                if line.strip() == "---":
                    body = "\n".join(parts[i + 1:])
                    for entry in parts[1:i]:
                        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", entry)
                        if m:
                            meta[m.group(1).lower()] = m.group(2).strip().strip("\"'")
                    return meta, body
    return meta, text


def _slug(path: Path) -> Optional[str]:
    sid = path.stem
    return sid if _SAFE_ID.match(sid) else None


def _md_to_html(text: str) -> Optional[str]:
    """Markdown → HTML (fenced_code, tables, nl2br). None, если markdown
    недоступен — тогда сайт отрендерит поле markdown на клиенте."""
    if _md is None:
        return None
    return _md.markdown(
        text,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        output_format="html5",
    )


def _collect(base: Path, kind_default_tag: str) -> list[dict]:
    items: list[dict] = []
    if not base.is_dir():
        return items
    for f in sorted(base.glob("*.md")):
        sid = _slug(f)
        if not sid:
            continue
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = _parse_frontmatter(raw)

        title = meta.get("title") or sid.replace("_", " ").replace("-", " ")
        # дата: из фронтматтера, иначе mtime файла
        date_iso: Optional[str] = None
        d_raw = meta.get("date")
        if d_raw:
            try:
                date_iso = datetime.fromisoformat(d_raw).date().isoformat()
            except ValueError:
                date_iso = None
        if date_iso is None:
            mt = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            date_iso = mt.date().isoformat()

        order = 0
        o_raw = meta.get("order")
        if o_raw:
            try:
                order = int(o_raw)
            except ValueError:
                order = 0

        items.append({
            "id": sid,
            "title": title,
            "date": date_iso,
            "order": order,
            "tag": meta.get("tag", kind_default_tag),
            "markdown": body.strip(),
            "html": _md_to_html(body.strip()),
        })
    return items


def list_news() -> list[dict]:
    """Новости: свежие сверху (по дате, затем по имени)."""
    items = _collect(NEWS_DIR, "News")
    items.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
    return items


def list_docs() -> list[dict]:
    """Документация: по order, затем по алфавиту имён файлов."""
    items = _collect(DOCS_DIR, "Docs")
    items.sort(key=lambda x: (x["order"], x["id"]))
    return items
