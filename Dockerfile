FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    SPOOLMIX_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

WORKDIR /app
# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY td_database.yaml catalog.yaml material_compatibility.yaml ./
RUN uv sync --frozen --no-dev

COPY docker/entrypoint.sh /usr/local/bin/spoolmix-entrypoint
RUN mkdir -p /data

LABEL org.opencontainers.image.title="spoolmix" \
      org.opencontainers.image.description="Plan multicolor 3D prints with filament color mixing" \
      org.opencontainers.image.source="https://github.com/lombarke/spoolmix" \
      org.opencontainers.image.licenses="MIT"

# Starts as root only to fix /data ownership, then drops to PUID:PGID (default 1000:1000).
VOLUME /data
EXPOSE 8790
ENTRYPOINT ["spoolmix-entrypoint"]
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8790/healthz')"
CMD ["uvicorn", "spoolmix.web.app:app", "--host", "0.0.0.0", "--port", "8790"]
