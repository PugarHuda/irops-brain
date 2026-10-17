# API + UI. Point it at FalkorDB with FALKORDB_HOST/PORT/USERNAME/PASSWORD (e.g. FalkorDB Cloud).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
# raw data, so the API can rebuild the graph on startup if the database comes back empty
RUN mkdir -p data/raw && python -c "import urllib.request as u; \
u.urlretrieve('https://transtats.bts.gov/PREZIP/On_Time_Reporting_Carrier_On_Time_Performance_1987_present_2022_12.zip', 'data/raw/bts_2022_12.zip'); \
u.urlretrieve('https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat', 'data/raw/airports.dat')"
COPY irops irops
COPY web web
COPY eval eval
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "irops.api:app", "--host", "0.0.0.0", "--port", "8000"]
