"""Proof that a lost worker can no longer act, so its step may be reconciled.

The execution ledger grants a recovery lease only on a ``QuiescenceProof``: the
old owner and every request it sent are settled. A lease that expired proves
neither (the process may be paused, not dead). In Kubernetes a worker's owner
ID is ``<pod name>.<pod uid>.<instance>`` (the instance is new at every
process start), and the proof is issued only when

* the database says the old lease has expired, and either
* the Kubernetes API says no pod with that name and UID is running (gone,
  replaced by a pod with another UID, or terminated); or
* the old owner is an earlier instance in this very pod. A pod runs one
  worker process, and the kubelet starts a container again only after the
  previous one has stopped, so while this process runs its predecessor
  cannot. (Without this a worker restarted in place could never recover its
  own step: its pod is running.)

In either case a separately registered observer must also prove that every
remote request from the old owner has settled. Process/container death does
not cancel a SQL request that the database has already received, nor settle
requests retained by a transport or relay. Without that observer, including
the default deployed configuration, no recovery proof is issued.

Anything uncertain issues nothing; the handoff stays in recovery for a
person. A proof is authenticated only by the instance that issued it and
only within its short validity.
"""

from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import ssl

import httpx

from sova.domain.contracts import ContractError, utc
from sova.domain.execution import QuiescenceProof, pending_digest

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_OWNER = re.compile(r"(?P<pod>[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?)\."
                    r"(?P<uid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
                    r"(?:\.(?P<instance>[0-9a-f]{8,32}))?")


def pod_owner(owner_id):
    """(pod name, pod uid, instance or None) of a pod-identified owner ID, else None."""
    found = _OWNER.fullmatch(owner_id or "")
    return (found["pod"], found["uid"], found["instance"]) if found else None


class KubernetesWorkerQuiescence:
    def __init__(self, *, pod_running, clock, validity=timedelta(minutes=2), this_owner=None,
                 requests_settled=None):
        """``pod_running(name, uid)``: True if that pod runs, False if it
        certainly does not, None when it cannot be told. ``this_owner`` is the
        owner ID of the worker using this verifier (its own pod and instance).
        ``requests_settled`` is a trusted observer of ALL remote channels. It
        returns the exact bool True only after the old owner's transports and
        remote effects are settled; absent/unknown/failed observations hold.
        A pg_stat_activity count or an application_name alone is insufficient.
        """
        if not callable(pod_running) or not callable(clock):
            raise ContractError("Pod state and trusted clock required")
        if requests_settled is not None and not callable(requests_settled):
            raise ContractError("Registered remote request settlement observer required")
        if not isinstance(validity, timedelta) or not timedelta(0) < validity <= timedelta(minutes=2):
            raise ContractError("Recovery proof validity must be positive and at most two minutes")
        self.pod_running, self.clock, self.validity = pod_running, clock, validity
        self.requests_settled = requests_settled
        self.this = pod_owner(this_owner) if this_owner is not None else None
        if this_owner is not None and (self.this is None or self.this[2] is None):
            raise ContractError("This worker's owner ID must name its pod and instance")
        self._issued = {}

    def prove(self, lifecycle, tenant, application_id, operation, pending):
        if self.requests_settled is None:
            return None
        with lifecycle.transaction() as conn:
            row = conn.execute(
                """SELECT owner_id, epoch, valid_until, clock_timestamp()
                FROM sova_control.app_execution WHERE tenant_id=%s AND application_id=%s
                AND operation_id=%s""",
                (tenant, application_id, operation)).fetchone()
        if row is None or row[0] is None:
            return None
        owner, epoch, until, now = row
        held = pod_owner(owner)
        if until > now or held is None:
            return None  # still leased, or not a pod-identified worker
        name, uid, instance = held
        if (self.this is not None and (name, uid) == self.this[:2] and instance is not None
                and instance != self.this[2]):
            state = "earlier instance in this pod"
        elif self.pod_running(name, uid) is False:
            state = "not running"
        else:
            return None
        bound_pending = pending_digest(pending)
        try:
            settled = self.requests_settled(tenant_id=tenant, application_id=application_id,
                operation_id=operation, owner_id=owner, epoch=epoch,
                pending_digest=bound_pending)
        except Exception:  # noqa: BLE001 - uncertainty cannot authorize recovery
            return None
        if settled is not True:
            return None
        observation = {"owner_id": owner, "pod": state, "lease_until": until.isoformat(),
                       "checked_at": now.isoformat(), "epoch": epoch,
                       "operation_id": operation, "pending_digest": bound_pending,
                       "remote_requests_settled": True}
        digest = hashlib.sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest()
        proof = QuiescenceProof(tenant, application_id, operation, epoch, pending_digest(pending),
                                digest, now, now + self.validity)
        self._issued[digest] = proof
        return proof

    def authenticate(self, proof):
        if self._issued.get(proof.artifact_digest) != proof:
            raise ContractError("Quiescence proof not issued here")
        if utc(self.clock()) >= proof.valid_until:
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
