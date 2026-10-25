FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

# Run as an unprivileged user
RUN useradd --system --uid 10001 --no-create-home otvet
COPY --chown=otvet:otvet app ./app
COPY --chown=otvet:otvet migrations ./migrations
COPY --chown=otvet:otvet alembic.ini ./alembic.ini
USER otvet

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# Migrations are applied by `docker compose run --rm app alembic upgrade head` (see docs/05-DEPLOY.md),
# not on every start, so a bad migration never hides behind a restart loop.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
