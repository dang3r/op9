FROM python:3.13-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy every module rather than listing them: naming files individually meant
# that adding agent.py silently shipped a broken image (ModuleNotFoundError at
# startup), because nothing local builds the container. Secrets and tests are
# excluded via .dockerignore.
COPY *.py ./

ENV PATH="/app/.venv/bin:$PATH"

CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}
