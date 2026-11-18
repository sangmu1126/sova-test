# Build ONLY with the allowlisted context from build_canary.py, never repo root.
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 AWS_EC2_METADATA_DISABLED=true
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock \
    && groupadd --gid 10001 canary \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin canary
COPY --chown=10001:10001 sova/ /app/sova/
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).read(2048)"]
CMD ["python", "-m", "uvicorn", "sova.qualification.order_runtime:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-proxy-headers", "--limit-concurrency", "24", "--backlog", "64", "--timeout-keep-alive", "3", "--timeout-graceful-shutdown", "8", "--no-server-header"]
