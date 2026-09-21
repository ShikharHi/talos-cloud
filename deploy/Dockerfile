# ─── Stage 1: Build & Dependencies ──────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build

# Install build prerequisites
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv for ultra-fast, reproducible dependency builds
RUN pip install --no-cache-dir uv

COPY pyproject.toml .
COPY uv.lock* ./

# Install python dependencies to virtual environment
RUN uv venv /opt/venv && \
    . /opt/venv/bin/activate && \
    uv pip install --no-cache -r <(uv export --format requirements-txt 2>/dev/null || pip list --format=freeze) || \
    uv pip install --no-cache .

# ─── Stage 2: Production Runtime ────────────────────────────────────────────
FROM python:3.13-slim AS runtime

WORKDIR /app

# Install runtime libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Create non-root system user
RUN groupadd -g 1001 talos && \
    useradd -u 1001 -g talos -s /bin/bash -m talos

# Copy application source
COPY --chown=talos:talos alembic /app/alembic
COPY --chown=talos:talos alembic.ini /app/alembic.ini
COPY --chown=talos:talos app /app/app
COPY --chown=talos:talos config /app/config
COPY --chown=talos:talos README.md /app/README.md

USER talos

EXPOSE 8000

# Default API entrypoint
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
