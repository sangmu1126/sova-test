# Build ONLY with the allowlisted context from build_canary.py, never repo root.
FROM python:3.12.16-alpine3.24@sha256:218ad6ba54faeb7f1089f391c597a80c9d69ca88e561f71ebd77fb452d39249b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 AWS_EC2_METADATA_DISABLED=true
WORKDIR /app
# Exact vendor-fixed version; apk verifies repository signatures. A missing
# patch fails the build rather than suppressing the scanner's findings.
RUN apk add --no-cache 'krb5-libs=1.22.3-r0' 'zlib=1.4-r0'
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock \
    && python -m pip uninstall --yes pip \
    && addgroup -g 10001 canary \
    && adduser -D -H -u 10001 -G canary -s /sbin/nologin canary
COPY --chown=10001:10001 sova/ /app/sova/
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).read(2048)"]
CMD ["python", "-m", "uvicorn", "sova.qualification.order_runtime:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-proxy-headers", "--limit-concurrency", "24", "--backlog", "64", "--timeout-keep-alive", "3", "--timeout-graceful-shutdown", "8", "--no-server-header"]
