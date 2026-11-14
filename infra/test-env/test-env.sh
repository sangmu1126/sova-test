#!/bin/sh
# The local integration-test environment: one disposable kind cluster holding
# PostgreSQL, two S3-compatible stores, two Keycloak servers and room for the
# Kubernetes tests. Everything listens on 127.0.0.1 only.
#
#   test-env.sh up         create the cluster and wait until every service is ready
#   test-env.sh env        print the variables the integration tests read
#   test-env.sh reset-db   restart PostgreSQL empty (the suite expects a fresh database)
#   test-env.sh status     show the pods
#   test-env.sh down       delete the cluster and everything in it
#
# The cluster's kubeconfig goes to its own file, never into ~/.kube/config, so
# the current kubectl context is left alone. Every credential here is a fixed,
# test-only value for loopback-bound disposable stores.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
CLUSTER=sova-test-env
KCFG=${SOVA_TEST_KUBECONFIG:-$HOME/.kube/sova-test-env.yaml}
k() { kubectl --kubeconfig "$KCFG" --context "kind-$CLUSTER" -n sova-test-env "$@"; }

wait_ready() {
  for deployment in postgres s3-source s3-target keycloak-source keycloak-target; do
    k rollout status "deployment/$deployment" --timeout=300s
  done
}

case "${1:-}" in
  up)
    if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
      mkdir -p "$(dirname "$KCFG")"
      kind create cluster --config "$HERE/kind-config.yaml" --kubeconfig "$KCFG"
    fi
    kubectl --kubeconfig "$KCFG" --context "kind-$CLUSTER" apply -f "$HERE/services.yaml"
    wait_ready
    echo "ready; run: eval \"\$($0 env)\""
    ;;
  env)
    cat <<EOF
export SOVA_TEST_KUBECONFIG="$KCFG"
export SOVA_TEST_KUBE_CONTEXT="kind-$CLUSTER"
export SOVA_TEST_POSTGRES_DSN="host=127.0.0.1 port=55432 user=postgres dbname=sova_test"
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
    k rollout restart deployment/postgres
    k rollout status deployment/postgres --timeout=120s
    until k exec deploy/postgres -- psql -U postgres -d sova_test -tAc "select 1" >/dev/null 2>&1; do
      sleep 1
    done
    ;;
  status)
    k get pods -o wide
    ;;
  down)
    kind delete cluster --name "$CLUSTER" --kubeconfig "$KCFG"
    ;;
  *)
    sed -n '2,13p' "$0"
    exit 2
    ;;
esac
