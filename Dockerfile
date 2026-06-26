# ─── Axiom Backend (Python 3.12 + FastAPI) ─────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Axiom Backend"
LABEL org.opencontainers.image.description="Algorithmic trading operations framework – paper-trade backend"
LABEL org.opencontainers.image.source="https://github.com/srossitto79/axiom"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system axiom && useradd --system --gid axiom --create-home axiom
WORKDIR /app

# ── Python dependencies ─────────────────────────────────────────────
COPY pyproject.toml ./
COPY axiom/ ./axiom/
COPY templates/ ./templates/

RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -e ".[discord]" \
    && pip install --no-cache-dir uvicorn[standard]

# ── Data directory ──────────────────────────────────────────────────
RUN mkdir -p /data && chown -R axiom:axiom /app /data

ENV AXIOM_HOME=/data
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

USER axiom
EXPOSE 8003

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fSs http://127.0.0.1:8003/api/health || exit 1

ENTRYPOINT ["python", "-m", "uvicorn", "--app-dir", ".", "axiom.api:app", "--host", "0.0.0.0", "--port", "8003"]
