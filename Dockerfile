# syntax=docker/dockerfile:1.7
# FuseIoT server image: REST API, dashboard, health, metrics, rules.
#
#   docker build -t fuseiot .
#   docker run -p 8080:8080 -e FUSEIOT_API_TOKEN=change-me fuseiot          # demo devices
#   docker run -p 8080:8080 -e FUSEIOT_API_TOKEN=... \
#              -v $PWD/fuseiot.yaml:/config/fuseiot.yaml:ro -v fuseiot-data:/data fuseiot

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY fuseiot ./fuseiot
RUN pip install --no-cache-dir --upgrade pip build \
 && python -m build --wheel --outdir /dist \
 && pip wheel --no-cache-dir --wheel-dir /wheels "$(ls /dist/*.whl)[all]"

FROM python:${PYTHON_VERSION}-slim
LABEL org.opencontainers.image.title="FuseIoT" \
      org.opencontainers.image.description="Deterministic device control server" \
      org.opencontainers.image.licenses="MIT"

RUN groupadd --system --gid 10001 fuseiot \
 && useradd --system --uid 10001 --gid fuseiot --home-dir /app --create-home fuseiot \
 && mkdir -p /config /data && chown fuseiot:fuseiot /data

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels fuseiot[all] && rm -rf /wheels

COPY --chown=fuseiot:fuseiot examples/demo.yaml /config/fuseiot.yaml

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FUSEIOT_CONFIG=/config/fuseiot.yaml \
    FUSEIOT_SERVER_HOST=0.0.0.0 \
    FUSEIOT_SERVER_PORT=8080 \
    FUSEIOT_LOG_JSON=true \
    FUSEIOT_PERSISTENCE_ENABLED=true \
    FUSEIOT_PERSISTENCE_PATH=/data/fuseiot.db \
    FUSEIOT_HISTORY_PATH=/data/history.db     FUSEIOT_USERS_DB=/data/users.db

USER fuseiot
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["fuseiot"]
CMD ["serve"]
