# syntax=docker/dockerfile:1.7
# The Sova control plane service image: API, worker and schema migration in
# one image, chosen by the command (sova-service serve-api|run-worker|migrate).
#
# Build with deploy/service/build-image.sh. The pinned sova-security wheel
# comes from the named build context "wheels" (a private index in CI).

# Python 3.12.16, pinned by digest; bump deliberately.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

FROM ${PYTHON_IMAGE} AS build
WORKDIR /src
COPY --from=wheels . /wheels/
COPY pyproject.toml ./
COPY src/ src/
RUN pip wheel --no-cache-dir --wheel-dir /wheels --find-links /wheels ".[service,aws,mysql]"

FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 sova \
 && useradd --system --uid 10001 --gid sova --no-create-home --shell /usr/sbin/nologin sova
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links /wheels "sova-control-plane[service,aws,mysql]" \
 && pip check
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8080 8081
ENTRYPOINT ["sova-service"]
