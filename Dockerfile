# One Dockerfile for every Python service in the uv workspace.
#   docker buildx build --build-arg SERVICE=core-api -t <repo>/core-api .
#
# Image layout (optimised for registry storage and node disk):
#   layer 1  python:3.13-slim                      shared by every service image
#   layer 2  third-party dependencies (+ model)    changes only when uv.lock changes
#   layer 3  our own packages + entrypoints        a few MB; the ONLY layer a normal commit changes
# So a commit pushes/pulls a few MB instead of a full ~1 GB virtualenv, and OCIR stores the big
# layer once. The OCI SDK is trimmed to the modules we use (it ships every OCI service: ~470 MB).
FROM python:3.13-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# only the manifests: this layer is reused until a dependency changes
COPY pyproject.toml uv.lock ./
COPY libs/telco_common/pyproject.toml libs/telco_common/pyproject.toml
COPY services/core-api/pyproject.toml services/core-api/pyproject.toml
COPY services/usage-collector/pyproject.toml services/usage-collector/pyproject.toml
COPY services/usage-aggregator/pyproject.toml services/usage-aggregator/pyproject.toml
COPY services/network-emulator/pyproject.toml services/network-emulator/pyproject.toml
ARG SERVICE
RUN test -n "$SERVICE" && uv sync --frozen --no-dev --no-install-workspace --package "$SERVICE"
# OCI SDK: keep auth/signing/config + the services we call (object storage, identity); drop the other ~150
COPY deploy/docker/trim_oci.py /tmp/trim_oci.py
RUN /app/.venv/bin/python /tmp/trim_oci.py /app/.venv/lib/python3.13/site-packages
# core-api: bake the local embedding model (all-MiniLM-L6-v2, ONNX, ~87 MB) so pods never download it
RUN mkdir -p /opt/models && if [ "$SERVICE" = "core-api" ]; then \
      /app/.venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2', cache_dir='/opt/models')"; \
    fi

FROM deps AS build
COPY libs libs
COPY services services
# install our packages, then copy ONLY the paths that didn't exist before (our code, its dist-info and
# the console-script entrypoints) into /delta -> the small final layer
RUN cd / && find app/.venv \( -type f -o -type l \) | sort > /tmp/before \
    && cd /app && uv sync --frozen --no-dev --no-editable --package "$SERVICE" \
    && cd / && find app/.venv \( -type f -o -type l \) | sort > /tmp/after \
    && comm -13 /tmp/before /tmp/after > /tmp/new && test -s /tmp/new \
    && mkdir -p /delta && tr '\n' '\0' < /tmp/new | xargs -0 cp -a --parents -t /delta \
    && echo "own layer: $(wc -l < /tmp/new) files"

FROM python:3.13-slim
RUN useradd --uid 10001 --create-home app
COPY --from=deps --chown=10001:10001 /app/.venv /app/.venv
COPY --from=deps --chown=10001:10001 /opt/models /opt/models
COPY --from=build --chown=10001:10001 /delta/app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    FASTEMBED_CACHE=/opt/models HF_HUB_OFFLINE=1
USER 10001
WORKDIR /app
