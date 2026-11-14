#!/bin/sh
# Run the whole suite the way this environment supports it:
#   1. everything against the plain PostgreSQL (tests of the read and write
#      gates, which accept only verified TLS, are skipped there);
#   2. exactly those gate tests against the PostgreSQL that requires verified TLS.
# Both servers are reset first, as the suite expects fresh databases.
#
#   SOVA_TEST_PYTHON=/path/to/python infra/test-env/run-suite.sh [pytest args]
#
# PYTHONPATH must already name the sources (src and the pinned security package).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
eval "$("$HERE/test-env.sh" env)"
PY=${SOVA_TEST_PYTHON:-python}
"$HERE/test-env.sh" reset-db >/dev/null
cd "$ROOT"
status=0
"$PY" -m pytest -q -p no:cacheprovider tests "$@" || status=$?
SOVA_TEST_POSTGRES_DSN="$SOVA_TEST_POSTGRES_TLS_DSN" \
  "$PY" -m pytest -q -p no:cacheprovider -m postgres_tls tests "$@" || status=$?
exit "$status"
