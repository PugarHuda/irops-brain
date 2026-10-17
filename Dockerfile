# API + UI. Point it at FalkorDB with FALKORDB_HOST/PORT/USERNAME/PASSWORD (e.g. FalkorDB Cloud).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY irops irops
COPY web web
COPY eval eval
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "irops.api:app", "--host", "0.0.0.0", "--port", "8000"]
