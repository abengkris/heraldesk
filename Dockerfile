# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY alembic ./alembic
RUN python -m pip install --prefix=/install .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HERALDESK_HOME=/data

RUN groupadd --gid 10001 heraldesk \
    && useradd --uid 10001 --gid heraldesk --create-home --home-dir /home/heraldesk heraldesk \
    && mkdir -p /data \
    && chown heraldesk:heraldesk /data

COPY --from=builder /install /usr/local

USER heraldesk
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

ENTRYPOINT ["heraldesk"]
CMD ["serve", "--host", "0.0.0.0", "--allow-remote"]
