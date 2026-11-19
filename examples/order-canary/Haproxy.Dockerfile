# Standard LTS data plane; configuration and leaf keys are mounted separately.
FROM haproxy:3.4.8-alpine3.24@sha256:ad1bb9e0745430a6126befde0e5d01b779a6eb28545d7f97074f4e4d9986772a
USER 0:0
# Exact vendor-fixed OpenSSL packages. Fail if unavailable; never suppress CVEs.
RUN apk add --no-cache 'libcrypto3=3.5.9-r1' 'libssl3=3.5.9-r1'
USER 10001:10001
