# Slim LLM gateway: LiteLLM proxy + Langfuse callback only (official image ~1.3 GB incl. Prisma/UI/enterprise
# extras and polars we don't use; the gateway runs without a database). Pinned, non-root.
#   docker build -f deploy/docker/gateway.Dockerfile -t <repo>/llm-gateway .
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY deploy/docker/gateway-requirements.txt /tmp/req.txt
RUN uv venv /opt/gw && uv pip install --python /opt/gw/bin/python --no-deps -r /tmp/req.txt \
    && /opt/gw/bin/python -c "import litellm.proxy.proxy_server, langfuse; print('gateway imports ok')"

FROM python:3.13-slim
RUN useradd --uid 10001 --create-home gw
COPY --from=build /opt/gw /opt/gw
ENV PATH=/opt/gw/bin:$PATH PYTHONUNBUFFERED=1 LITELLM_LOG=WARNING
USER 10001
EXPOSE 4000
ENTRYPOINT ["litellm"]
