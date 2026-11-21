# One Dockerfile for every Python service in the uv workspace.
#   docker buildx build --platform linux/arm64 --build-arg SERVICE=core-api -t <repo>/core-api .
# Only the selected package and its dependencies are installed (uv --package), so each image
# stays minimal; runs as non-root with a read-only root filesystem in Kubernetes.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
ARG SERVICE
RUN test -n "$SERVICE" && uv sync --frozen --no-dev --no-editable --package "$SERVICE"
# core-api: bake the local embedding model (all-MiniLM-L6-v2, ONNX, ~87 MB) so pods never download it
RUN mkdir -p /opt/models && if [ "$SERVICE" = "core-api" ]; then \
      /app/.venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2', cache_dir='/opt/models')"; \
    fi

FROM python:3.13-slim
RUN useradd --uid 10001 --create-home app
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
COPY --from=build --chown=10001:10001 /opt/models /opt/models
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    FASTEMBED_CACHE=/opt/models HF_HUB_OFFLINE=1
USER 10001
WORKDIR /app
