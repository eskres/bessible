# syntax=docker/dockerfile:1
FROM python:3.15-slim-bookworm

# Copy uv binary from official image
COPY --from=ghcr.io/astral-sh/uv:0.6.5 /uv /uvx /bin/

# Environment configurations
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install curl for container health checks and ca-certificates for secure outbound API calls
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications first for optimal Docker layer caching
COPY pyproject.toml uv.lock ./

# Install project dependencies without the application code
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source code, dataset fixtures, and metadata
COPY src ./src
COPY data ./data
COPY README.md ./

# Install the application package into the virtual environment
RUN uv sync --frozen --no-dev

# Set up runtime directories and create non-root user for security
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/out /app/data /var/lib/bessible && \
    chown -R appuser:appuser /app /var/lib/bessible

USER appuser

EXPOSE 8000

# Health check against FastAPI health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default command runs the FastAPI API server.
# To run the Temporal worker, override with: python -m bessible.worker
CMD ["uvicorn", "bessible.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
