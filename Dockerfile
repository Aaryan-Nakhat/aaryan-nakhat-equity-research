# Equity Research Workbench — one container: the web UI (+ the email bot if configured in .env).
#
#   docker compose up -d                      # then open http://localhost:8765
#   docker compose run --rm eqr eqr demo --yes --no-serve   # first run: fetch a starter set
#
# Data (the DuckDB file, saved reports, logs) lives in the /data volume. Config comes from .env.
# The heavy NSE browser tier (Camoufox) is NOT in the image by default — build with
# `--build-arg WITH_NSE_BROWSER=true` if you'll enable NSE_SCRAPING_ENABLED.
FROM python:3.12-slim

ARG UV_EXTRAS=""
ARG WITH_NSE_BROWSER=false

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    EQR_DB_PATH=/data/equity.duckdb \
    EQR_OUTPUT_DIR=/data/outputs \
    EQR_IN_CONTAINER=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8765

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app

# dependencies first (cached layer), then the project
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project ${UV_EXTRAS}
COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev ${UV_EXTRAS}

# Chromium for the report PDFs (+ its system libraries); optionally the NSE browser tier
RUN uv run --no-sync playwright install --with-deps chromium \
 && if [ "$WITH_NSE_BROWSER" = "true" ]; then uv run --no-sync scrapling install; fi \
 && rm -rf /var/lib/apt/lists/*

VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4)" || exit 1

CMD ["uv", "run", "--no-sync", "eqr", "serve"]
