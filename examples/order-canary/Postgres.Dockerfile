# Same PG major/minor as the local regression; actual artifact requalified.
FROM postgres:17.12-alpine3.24@sha256:e5f8ac607c7d44a0da3a42712a3e978b75fb1757e9f3e1488cd0cca4dbec76e9
# The upstream root-to-postgres helper contains an old Go runtime. This
# deployment never starts PostgreSQL as root, so remove the unused helper.
# Data/config ownership MUST be prepared separately before a non-root start.
RUN apk add --no-cache 'libcrypto3=3.5.9-r1' 'libssl3=3.5.9-r1' \
    && test -f /usr/local/bin/gosu && rm /usr/local/bin/gosu
USER 70:70
