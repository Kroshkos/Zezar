FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY modules/ ./modules/
COPY integrations/ ./integrations/
COPY scripts/ ./scripts/
COPY static/ ./static/

# TODO(alembic): добавить alembic.ini и alembic/ при переходе на миграции вместо create_all

# Не-руководитель (root) не нужен — монтируем /data от своего uid при желании
RUN useradd -m appuser && mkdir -p /data && chown -R appuser:appuser /data /srv/app
USER appuser

# БД SQLite строго в /data (том контейнера), путь переопределяется через .env/DATABASE_PATH
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
