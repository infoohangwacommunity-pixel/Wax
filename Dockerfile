# Dockerfile for WAX runtime.
#
# Used by Railway (and any container host) to build a production image.
# Uses Python 3.12-slim as the base. Image runs as non-root.

FROM python:3.12-slim AS base

# Avoid Python writing .pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install build deps for asyncpg (libpq-dev) and uv for fast installs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv

WORKDIR /app

# Install dependencies first (cache layer).
# uv reads pyproject.toml and resolves the full dependency tree.
COPY pyproject.toml alembic.ini ./
COPY src ./src
RUN uv pip install --system -e .

# Copy migrations and docs last (they change more often).
COPY migrations ./migrations
COPY README.md ./

# Create a non-root user. The runtime will run as this user.
RUN useradd --create-home --shell /bin/bash wax \
    && chown -R wax:wax /app
USER wax

EXPOSE 8000

# Railway injects $PORT. Default to 8000 for local docker runs.
ENV PORT=8000

# Run migrations on every deploy, then start the ASGI server.
CMD ["sh", "-c", "alembic upgrade head && uvicorn wax.runtime.asgi:app --host 0.0.0.0 --port ${PORT:-8000}"]
