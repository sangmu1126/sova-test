"""Read the personal entry through native controls and fresh actual HTTPS.

A bounded ServiceAccount token stays in memory. No patch, SQL mutation, exec,
secret read or traffic switch is performed. Namespace identity is rechecked.
"""

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path

from import_target_images import CONFIG, KUBECTL, guest, identity
from sova.adapters.kubernetes.handoff_controls import KubernetesHandoffControls, KubernetesServiceRoute
from sova.adapters.kubernetes.gateway_route import http_side_observer
from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.domain.discovery import KubernetesReadRegistration


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--entry-connection", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    identity()
    inputs = json.loads((args.inputs / "identity.json").read_text())
    run, namespace = inputs["run_id"], inputs["namespace"]
    if run != "b0be270e62" or namespace != "sj-b0be270e62":
        raise ValueError("Only the owned personal qualification run is allowed")
    now = datetime.now(UTC)
    if now >= datetime.fromisoformat(inputs["expires_at"]):
        raise ValueError("Canary credentials expired")
    found = json.loads(guest(KUBECTL, "--kubeconfig", CONFIG, "get", "ns", namespace, "-o", "json"))
    if found["metadata"]["uid"] != "565b91c4-4f6e-4d06-94c6-9f8803afa6c9":
        raise ValueError("Destination namespace identity changed")
    ca = guest("cat", "/var/lib/rancher/rke2/server/tls/server-ca.crt")
    token = guest(KUBECTL, "--kubeconfig", CONFIG, "-n", namespace, "create", "token",
                  "orders-route-worker", "--duration=10m").decode().strip()
    registration = KubernetesReadRegistration(
        tenant_id="personal", application_id="orders-b0be270e62", environment_id="local-vm-b0be270e62",
        location="registered-personal-vm", endpoint="https://127.0.0.1:19753",
        approved_addresses=("127.0.0.1",), ca_digest=hashlib.sha256(ca).hexdigest(),
        namespace=namespace, namespace_uid=found["metadata"]["uid"], revision=1,
        registered_at=now, valid_until=min(now + timedelta(minutes=10), datetime.fromisoformat(inputs["expires_at"])))
    controls = KubernetesHandoffControls(registration, ca_pem=ca, token_provider=lambda: token)
    entry = json.loads(args.entry_connection.read_text())
    service = controls.service("orders-entry")
    if service["metadata"]["uid"] != entry["service_uid"]:
        raise ValueError("Entry Service replaced")
    observe = http_side_observer(f"https://127.0.0.1:{entry['port']}/healthz", header="X-Sova-Side",
        sides={"source_orders": "source", "target_orders": "target"}, ca_file=str(args.inputs / "pki/ca.pem"))
    route = KubernetesServiceRoute(controls, "orders-entry", {
        side + "_orders": {"sova.run": run, "sova.role": "edge", "sova.side": side}
        for side in ("source", "target")}, observe=observe, samples=3)
    seen = route.observe()
    if seen["route"] != "source_orders":
        raise ValueError("Actual entry is not unanimously source")
    denied = {}
    for name, path in (("secret_read", f"/api/v1/namespaces/{namespace}/secrets/orders-app-input-v2"),
                       ("other_service_read", f"/api/v1/namespaces/{namespace}/services/orders-db"),
                       ("other_namespace", "/api/v1/namespaces/default/pods")):
        try:
            controls._request("GET", path, {}, timeout=5)
        except KubernetesRequestRefused as exc:
            if exc.status != 403:
                raise
            denied[name] = "403"
        else:
            raise ValueError("Route worker has unexpected privileges")
    report = {"observed_at": datetime.now(UTC).isoformat(), "run_id": run,
        "namespace_uid": registration.namespace_uid, "service_uid": service["metadata"]["uid"],
        "registration_digest": registration.fingerprint, "route": seen,
        "refusals": denied, "token_lifetime_seconds": 600,
        "traffic_changed": False, "business_data_changed": False,
        "migration_complete": False, "operational_qualification": False}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"route": seen, "refusals": denied, "migration_complete": False}))


if __name__ == "__main__":
    main()
