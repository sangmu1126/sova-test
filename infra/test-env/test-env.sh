#!/bin/sh
# The local integration-test environment: one disposable kind cluster holding
# PostgreSQL (plain, and a second one that requires verified TLS), two
# S3-compatible stores, two Keycloak servers, MySQL, RabbitMQ, Redpanda (Kafka
# API) and room for the Kubernetes tests.
# Everything listens on 127.0.0.1 only.
#
#   test-env.sh up         create the cluster and wait until every service is ready
#   test-env.sh env        print the variables the integration tests read
#   test-env.sh reset-db   restart both PostgreSQL servers empty (the suite expects
#                          a fresh database)
#   test-env.sh gateway    add Envoy Gateway (a Gateway API implementation) for the
#                          entry-point route tests; pinned manifest and image
#   test-env.sh second     add a second, empty kind cluster (its own API server and
#                          kubeconfig) for handoffs between two clusters
#   test-env.sh down-second  delete the second cluster
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
# Envoy Gateway: the release's install manifest (Gateway API CRDs included),
# checked against its SHA-256, and the controller image pinned by digest.
EG_VERSION=v1.9.3
EG_MANIFEST_SHA256=ac89b0b564e52c97c97a3f8b4d82bb594a0fbc810431fb2e6ffd91702aabebfb
EG_IMAGE=docker.io/envoyproxy/gateway:v1.9.3@sha256:192fc9bdadb6d2462e8cf7bd702ce3dd9648f3310c6038787448a1f509b7cd9f
CLUSTER=sova-test-env
KCFG=${SOVA_TEST_KUBECONFIG:-$HOME/.kube/sova-test-env.yaml}
SECOND=sova-test-env-b
KCFG_B=${SOVA_TEST_KUBECONFIG_B:-$HOME/.kube/sova-test-env-b.yaml}
PYTHON_IMAGE=docker.io/library/python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
STATE=${SOVA_TEST_ENV_STATE:-$HOME/.sova-test-env}
TLS="$STATE/tls"
k() { kubectl --kubeconfig "$KCFG" --context "kind-$CLUSTER" -n sova-test-env "$@"; }

wait_ready() {
  for deployment in postgres postgres-tls s3-source s3-target keycloak-source keycloak-target \
      mysql rabbitmq redpanda; do
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
export SOVA_TEST_MYSQL="127.0.0.1:53306"
export SOVA_TEST_MYSQL_ROOT="root:sova-mysql-test-secret"
export SOVA_TEST_RABBITMQ="127.0.0.1:55672"
export SOVA_TEST_RABBITMQ_HTTP="http://127.0.0.1:55673"
export SOVA_TEST_RABBITMQ_ADMIN="sova-admin:sova-rabbit-test-secret"
export SOVA_TEST_KAFKA="127.0.0.1:59092"
export SOVA_TEST_KAFKA_ADMIN_API="http://127.0.0.1:59644"
export SOVA_TEST_KAFKA_ADMIN="sova-admin:sova-kafka-test-secret"
EOF
    if kind get clusters 2>/dev/null | grep -qx "$SECOND"; then
      printf 'export SOVA_TEST_KUBECONFIG_B="%s"\nexport SOVA_TEST_KUBE_CONTEXT_B="kind-%s"\n' \
        "$KCFG_B" "$SECOND"
    fi
    ;;
  second)
    if ! kind get clusters 2>/dev/null | grep -qx "$SECOND"; then
      mkdir -p "$(dirname "$KCFG_B")"
      kind create cluster --name "$SECOND" --kubeconfig "$KCFG_B"
    fi
    # The agents' and the test applications' image, by digest, on the new node.
    docker exec "$SECOND-control-plane" crictl pull "$PYTHON_IMAGE" >/dev/null
    kubectl --kubeconfig "$KCFG_B" --context "kind-$SECOND" wait --for=condition=Ready \
      node --all --timeout=180s
    ;;
  down-second)
    kind delete cluster --name "$SECOND" --kubeconfig "$KCFG_B"
    ;;
  reset-db)
    k rollout restart deployment/postgres deployment/postgres-tls
    k rollout status deployment/postgres --timeout=120s
    k rollout status deployment/postgres-tls --timeout=120s
    wait_sql postgres
    wait_sql postgres-tls
    ;;
  gateway)
    # SOVA_GATEWAY_KUBECONFIG / SOVA_GATEWAY_CONTEXT: install the same pinned
    # release on another test cluster (a cloud run's managed one).
    kc() { kubectl --kubeconfig "${SOVA_GATEWAY_KUBECONFIG:-$KCFG}" \
      --context "${SOVA_GATEWAY_CONTEXT:-kind-$CLUSTER}" "$@"; }
    manifest="$STATE/gateway/envoy-gateway-$EG_VERSION.yaml"
    if [ ! -s "$manifest" ]; then
      mkdir -p "$STATE/gateway"
      curl -fsSL -o "$manifest.part" \
        "https://github.com/envoyproxy/gateway/releases/download/$EG_VERSION/install.yaml"
      mv "$manifest.part" "$manifest"
    fi
    echo "$EG_MANIFEST_SHA256  $manifest" | shasum -a 256 -c -
    kc apply --server-side -f "$manifest"
    kc -n envoy-gateway-system set image deployment/envoy-gateway "envoy-gateway=$EG_IMAGE"
    kc -n envoy-gateway-system set resources deployment/envoy-gateway \
      --requests=cpu=50m,memory=128Mi
    kc -n envoy-gateway-system rollout status deployment/envoy-gateway --timeout=300s
    kc apply -f "$HERE/gateway-class.yaml"
    kc wait --for=condition=Accepted gatewayclass/sova-envoy --timeout=180s
    ;;
  status)
    k get pods -o wide
    ;;
  down)
    kind delete cluster --name "$CLUSTER" --kubeconfig "$KCFG"
    ;;
  *)
    sed -n '2,23p' "$0"
    exit 2
    ;;
esac
