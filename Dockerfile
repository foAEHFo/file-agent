FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKSPACE_SEED_PATH=/app/workspace \
    RUNTIME_ROOT=/tmp/file-agent

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

COPY workspace ./workspace

RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["sh", "-c", "exec uvicorn file_agent.web:app --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1"]
