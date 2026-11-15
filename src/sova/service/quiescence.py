"""Proof that a lost worker can no longer act, so its step may be reconciled.

The execution ledger grants a recovery lease only on a ``QuiescenceProof``: the
old owner and every request it sent are settled. A lease that expired proves
neither (the process may be paused, not dead). In Kubernetes a worker's owner
ID is ``<pod name>.<pod uid>``, and the proof is issued only when

* the database says the old lease has expired, and
* the Kubernetes API says no pod with that name and UID is running (gone,
  replaced by a pod with another UID, or terminated): its containers have
  stopped, so no request from it can still arrive.

Anything uncertain (the API unreachable, the pod still running, an owner ID
that names no pod) issues nothing; the handoff stays in recovery for a
person. A proof is authenticated only by the instance that issued it and
only within its short validity.
"""

from datetime import timedelta
import hashlib
import json
from pathlib import Path
import ssl

import httpx

from sova.domain.contracts import ContractError, utc
from sova.domain.execution import QuiescenceProof, pending_digest

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class KubernetesWorkerQuiescence:
    def __init__(self, *, pod_running, clock, validity=timedelta(minutes=2)):
        """``pod_running(name, uid)``: True if that pod runs, False if it
        certainly does not, None when it cannot be told."""
        if not callable(pod_running) or not callable(clock):
            raise ContractError("Pod state and trusted clock required")
        self.pod_running, self.clock, self.validity = pod_running, clock, validity
        self._issued = {}

    def prove(self, lifecycle, tenant, application_id, operation, pending):
        with lifecycle.transaction() as conn:
            row = conn.execute(
                """SELECT owner_id, epoch, valid_until, clock_timestamp()
                FROM sova_control.app_execution WHERE tenant_id=%s AND application_id=%s""",
                (tenant, application_id)).fetchone()
        if row is None or row[0] is None:
            return None
        owner, epoch, until, now = row
        if until > now or "." not in owner:
            return None  # still leased, or not a pod-identified worker
        name, uid = owner.rsplit(".", 1)
        if self.pod_running(name, uid) is not False:
            return None
        observation = {"owner_id": owner, "pod": "not running", "lease_until": until.isoformat(),
                       "checked_at": now.isoformat(), "epoch": epoch}
        digest = hashlib.sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest()
        proof = QuiescenceProof(tenant, application_id, operation, epoch, pending_digest(pending),
                                digest, now, now + self.validity)
        self._issued[digest] = proof
        return proof

    def authenticate(self, proof):
        if self._issued.get(proof.artifact_digest) != proof:
            raise ContractError("Quiescence proof not issued here")
        if utc(self.clock()) > proof.valid_until:
            raise ContractError("Quiescence proof expired")


def in_cluster_pod_state(namespace, *, timeout=5.0, root=SERVICE_ACCOUNT):
    """``pod_running`` against this cluster's API with the pod's own service
    account (RBAC: get pods in ``namespace``)."""
    token_file, ca_file = root / "token", root / "ca.crt"
    context = ssl.create_default_context(cafile=str(ca_file))

    def pod_running(name, uid):
        try:
            response = httpx.get(
                f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/pods/{name}",
                headers={"Authorization": "Bearer " + token_file.read_text().strip()},
                verify=context, timeout=timeout)
        except httpx.HTTPError:
            return None
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            return None
        pod = response.json()
        if pod.get("metadata", {}).get("uid") != uid:
            return False  # the name now belongs to another pod
        return pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")

    return pod_running
