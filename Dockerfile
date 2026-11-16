# Demo mode image (docs/demo-mode.md). Runs Mochi on fictional fixture data —
# no real credentials, no Telegram, no personal data. The same image backs both
# the `seed` and `app` services in docker-compose.yml.
FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Dependencies first (layer cache): the lockfile pins every version.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Then the code + demo fixtures + the Ollama Modelfile.
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY demo/ ./demo/
COPY ollama/ ./ollama/

ENV PYTHONPATH=/app

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

CMD ["uv", "run", "python", "-m", "app.main"]
