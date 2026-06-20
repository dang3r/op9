FROM python:3.13-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app.py config.py ./

ENV PATH="/app/.venv/bin:$PATH"

CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}
