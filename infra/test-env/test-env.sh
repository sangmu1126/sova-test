#!/bin/sh
# The local integration-test environment: one disposable kind cluster holding
# PostgreSQL (plain, and a second one that requires verified TLS), two
# S3-compatible stores, two Keycloak servers and room for the Kubernetes tests.
# Everything listens on 127.0.0.1 only.
#
#   test-env.sh up         create the cluster and wait until every service is ready
#   test-env.sh env        print the variables the integration tests read
#   test-env.sh reset-db   restart both PostgreSQL servers empty (the suite expects
#                          a fresh database)
#   test-env.sh status     show the pods
#   test-env.sh down       delete the cluster and everything in it
#
# The cluster's kubeconfig goes to its own file, never into ~/.kube/config, so
# the current kubectl context is left alone. Every credential here is a fixed,
# test-only value for loopback-bound disposable stores; the TLS test CA and
# server key are generated locally under $SOVA_TEST_ENV_STATE (default
# ~/.sova-test-env) and never leave this machine.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
CLUSTER=sova-test-env
KCFG=${SOVA_TEST_KUBECONFIG:-$HOME/.kube/sova-test-env.yaml}
STATE=${SOVA_TEST_ENV_STATE:-$HOME/.sova-test-env}
TLS="$STATE/tls"
k() { kubectl --kubeconfig "$KCFG" --context "kind-$CLUSTER" -n sova-test-env "$@"; }

wait_ready() {
  for deployment in postgres postgres-tls s3-source s3-target keycloak-source keycloak-target; do
    k rollout status "deployment/$deployment" --timeout=300s
  done
}

tls_material() {
  [ -s "$TLS/ca.pem" ] && [ -s "$TLS/server.crt" ] && return 0
  mkdir -p "$TLS" && chmod 700 "$STATE" "$TLS"
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=sova-test-env CA" \
    -keyout "$TLS/ca.key" -out "$TLS/ca.pem" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=localhost" \
    -keyout "$TLS/server.key" -out "$TLS/server.csr" 2>/dev/null
  printf 'subjectAltName=DNS:localhost,IP:127.0.0.1\n' > "$TLS/server.ext"
  openssl x509 -req -in "$TLS/server.csr" -CA "$TLS/ca.pem" -CAkey "$TLS/ca.key" \
    -CAcreateserial -days 365 -extfile "$TLS/server.ext" -out "$TLS/server.crt" 2>/dev/null
  chmod 600 "$TLS"/*.key
}

wait_sql() {
  until k exec "deploy/$1" -- psql -U postgres -d sova_test -tAc "select 1" >/dev/null 2>&1; do
    sleep 1
  done
}

case "${1:-}" in
  up)
    if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
      mkdir -p "$(dirname "$KCFG")"
      kind create cluster --config "$HERE/kind-config.yaml" --kubeconfig "$KCFG"
    fi
    kubectl --kubeconfig "$KCFG" --context "kind-$CLUSTER" apply -f "$HERE/services.yaml"
    tls_material
    k create secret generic postgres-tls --from-file="$TLS/server.crt" \
      --from-file="$TLS/server.key" --from-file="$TLS/ca.pem" --dry-run=client -o yaml \
      | k apply -f -
    sed "s|__TLS_DIR__|$TLS|g" "$HERE/postgres-tls.yaml" \
      | kubectl --kubeconfig "$KCFG" --context "kind-$CLUSTER" apply -f -
    wait_ready
    echo "ready; run: eval \"\$($0 env)\""
    ;;
  env)
    cat <<EOF
export SOVA_TEST_KUBECONFIG="$KCFG"
export SOVA_TEST_KUBE_CONTEXT="kind-$CLUSTER"
export SOVA_TEST_POSTGRES_DSN="host=127.0.0.1 port=55432 user=postgres dbname=sova_test"
export SOVA_TEST_POSTGRES_TLS_DSN="host=localhost hostaddr=127.0.0.1 port=55433 user=postgres dbname=sova_test sslmode=verify-full sslrootcert=$TLS/ca.pem"
export SOVA_TEST_POSTGRES_TLS_ROOT="$TLS"
export SOVA_TEST_POSTGRES_EXEC="kubectl --kubeconfig $KCFG --context kind-$CLUSTER -n sova-test-env exec -i deploy/postgres --"
export SOVA_TEST_S3_ENDPOINTS="http://127.0.0.1:57070,http://127.0.0.1:57071"
export SOVA_TEST_S3_ROOT="sovaroot:sova-root-test-secret"
export SOVA_TEST_S3_WRITERS="appuser:appuser-test-secret,batchuser:batchuser-test-secret"
export SOVA_TEST_S3_PLATFORM="platformuser:platformuser-test-secret"
export SOVA_TEST_KEYCLOAK_URLS="http://127.0.0.1:58080,http://127.0.0.1:58081"
export SOVA_TEST_KEYCLOAK_ADMIN="sova-admin:sova-admin-test-secret"
EOF
    ;;
  reset-db)
    k rollout restart deployment/postgres deployment/postgres-tls
    k rollout status deployment/postgres --timeout=120s
    k rollout status deployment/postgres-tls --timeout=120s
    wait_sql postgres
    wait_sql postgres-tls
    ;;
  status)
    k get pods -o wide
    ;;
  down)
    kind delete cluster --name "$CLUSTER" --kubeconfig "$KCFG"
    ;;
  *)
    sed -n '2,18p' "$0"
    exit 2
    ;;
esac
