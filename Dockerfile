# Headless ReallymyRadar for Linux servers / NAS boxes / Raspberry Pi 4-5 (arm64) / anything with Docker.
#   docker build -t reallymyradar .
#   docker run -d --name radar -p 8600:8600 -v radar-data:/data reallymyradar

# Build stage: Py-ART has no prebuilt arm64 Linux wheel, so it may compile here.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY reallymyradar ./reallymyradar
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH RMR_HOME=/data PYART_QUIET=1
VOLUME /data
EXPOSE 8600
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8600/api/status', timeout=4)"
CMD ["radar"]
