FROM ghcr.io/astral-sh/uv:0.13.6 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable \
    && useradd --no-create-home --uid 10001 simulator
USER simulator
ENTRYPOINT ["/app/.venv/bin/uvicorn", "trading_platform.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
