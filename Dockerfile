FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY modules/ ./modules/
COPY integrations/ ./integrations/
COPY scripts/ ./scripts/
COPY static/ ./static/
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY entrypoint.sh /srv/app/entrypoint.sh
RUN chmod +x /srv/app/entrypoint.sh

# Не-руководитель (root) не нужен — монтируем /data от своего uid при желании
RUN useradd -m appuser && mkdir -p /data && chown -R appuser:appuser /data /srv/app
USER appuser

ENV PYTHONPATH=/srv/app

# БД SQLite строго в /data (том контейнера), путь переопределяется через .env/DATABASE_PATH
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["sh", "-c", "/srv/app/entrypoint.sh"]
