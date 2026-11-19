"""Whether another workload can reach an application's Secrets on a Kubernetes
target (SEC-01.1; the target of the secrets handoff).

On Kubernetes a Secret is reached two ways: through the API (``get``,
``list`` and ``watch`` of Secrets) and by running a workload in its namespace,
since a Pod may mount any Secret of its namespace and the kubelet hands it
over without asking whether the Pod's service account may read it. So a
principal that may create or change Pods, or anything that makes Pods
(Deployments, StatefulSets, DaemonSets, ReplicaSets, Jobs, CronJobs), in the
namespace can read every Secret there.

Each other principal - a service account of another workload, here or in
another namespace - is measured with two views that must agree (ADR-0007):

* attempts with that principal's own token: ``get`` of every registered
  Secret, ``list`` of the namespace's Secrets, and a server-side dry run of a
  Pod that mounts the first Secret (authorised and admitted like a real one,
  never stored). The Pod meets the restricted Pod Security profile, so a
  refusal is the principal's, not the profile's;
* the API server's own decision for the same principal and its groups
  (``SubjectAccessReview``): ``get``, ``list`` and ``watch`` of Secrets, and
  ``create``, ``update`` and ``patch`` of every workload kind above. The
  groups matter: a binding to ``system:serviceaccounts`` reaches every
  service account.

The positive control is the probe's reader: every registered Secret is there,
before and after. A refusal counts only as HTTP 403; a principal whose token
is not accepted (401) is undecided, never refused. Observations name
principals and Secrets, never a value.
"""

from dataclasses import dataclass
import json
import re

from sova.adapters.kubernetes.https import KubernetesHTTPS, KubernetesRequestRefused
from sova.domain.contracts import ContractError

_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
_TIMEOUT = 5
WORKLOADS = (("", "pods"), ("apps", "deployments"), ("apps", "statefulsets"),
             ("apps", "daemonsets"), ("apps", "replicasets"), ("batch", "jobs"),
             ("batch", "cronjobs"))


@dataclass(frozen=True)
class ServiceAccount:
    namespace: str
    name: str

    def __post_init__(self):
        if not all(type(v) is str and _NAME.fullmatch(v) for v in (self.namespace, self.name)):
            raise ContractError("A service account's namespace and name required")

    @property
    def user(self):
        return f"system:serviceaccount:{self.namespace}:{self.name}"

    @property
    def groups(self):
        return ["system:serviceaccounts", f"system:serviceaccounts:{self.namespace}",
                "system:authenticated"]


class SecretAccessControls(KubernetesHTTPS):
    """The exact calls the probe makes in the registered namespace."""

    def __init__(self, registration, *, ca_pem, token_provider):
        super().__init__(registration, ca_pem=ca_pem, token_provider=token_provider)
        self.ca_pem = ca_pem  # the other accounts' clients pin the same CA

    def _path(self, plural, name=None):
        path = f"/api/v1/namespaces/{self.registration.namespace}/{plural}"
        if name is not None:
            if type(name) is not str or not _NAME.fullmatch(name):
                raise ContractError("A Kubernetes object name required")
            path += "/" + name
        return path

    def _status(self, method, path, query=None, body=None):
        try:
            self._request(method, path, query or {}, timeout=_TIMEOUT, body=body,
                          content_type="application/json" if body is not None else None,
                          accept=(200, 201))
            return 200
        except KubernetesRequestRefused as refused:
            return refused.status

    def secret_status(self, name):
        """200 when this principal may read the Secret (its value is dropped)."""
        return self._status("GET", self._path("secrets", name))

    def list_status(self):
        return self._status("GET", self._path("secrets"), {"limit": "1"})

    def pod_dry_run_status(self, secret):
        """A Pod mounting ``secret``, created with ``dryRun=All``: authorised
        and admitted as a real one would be, never stored."""
        pod = {"apiVersion": "v1", "kind": "Pod",
               "metadata": {"generateName": "sova-secret-access-check-"},
               "spec": {"automountServiceAccountToken": False,
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 65534,
                                            "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [{
                            "name": "check", "image": "registry.invalid/never-pulled:dry-run",
                            "securityContext": {"allowPrivilegeEscalation": False,
                                                "capabilities": {"drop": ["ALL"]}},
                            "volumeMounts": [{"name": "s", "mountPath": "/s",
                                              "readOnly": True}]}],
                        "volumes": [{"name": "s", "secret": {"secretName": secret}}]}}
        return self._status("POST", self._path("pods"), {"dryRun": "All"},
                            json.dumps(pod).encode())

    def token_for(self, account, *, seconds=600):
        """A short-lived token of ``account`` (TokenRequest), for its own
        attempts: the platform needs ``create`` on ``serviceaccounts/token``
        for exactly the registered accounts. The token stays in memory."""
        if not isinstance(account, ServiceAccount) or type(seconds) is not int or not (
                600 <= seconds <= 3600):
            raise ContractError("A registered service account and 600 to 3600 seconds")
        body = json.dumps({"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest",
                           "spec": {"expirationSeconds": seconds}}).encode()
        answer = self._request(
            "POST", f"/api/v1/namespaces/{account.namespace}/serviceaccounts/{account.name}/token",
            {}, timeout=_TIMEOUT, body=body, content_type="application/json", accept=(200, 201))
        token = (answer.get("status") or {}).get("token")
        if type(token) is not str or not token:
            raise ContractError("No token issued for the service account")
        return token

    def access_review(self, account, verb, group, resource, name=None):
        """The API server's decision for ``account`` (with its groups)."""
        attributes = {"namespace": self.registration.namespace, "verb": verb,
                      "group": group, "resource": resource, **({"name": name} if name else {})}
        body = json.dumps({"apiVersion": "authorization.k8s.io/v1",
                           "kind": "SubjectAccessReview",
                           "spec": {"user": account.user, "groups": account.groups,
                                    "resourceAttributes": attributes}}).encode()
        answer = self._request("POST", "/apis/authorization.k8s.io/v1/subjectaccessreviews",
                               {}, timeout=_TIMEOUT, body=body,
                               content_type="application/json", accept=(200, 201))
        status = answer.get("status") or {}
        return bool(status.get("allowed")) and not status.get("denied")


def _outcome(status):
    if status == 200:
        return "allowed"
    if status == 403:
        return "refused"
    return f"undecided:{status}"


class KubernetesSecretProbes:
    def __init__(self, *, reader, reviewer, as_account, secrets, others):
        """``reader`` and ``reviewer`` are SecretAccessControls of the platform
        (read the Secrets; create SubjectAccessReviews); ``as_account(account)``
        the same calls with that service account's own token."""
        if not all(isinstance(c, SecretAccessControls) for c in (reader, reviewer)):
            raise ContractError("Registered Secret access controls required")
        if not callable(as_account):
            raise ContractError("A client per other service account required")
        secrets = tuple(secrets or ())
        others = tuple(others or ())
        if not secrets or not all(type(s) is str and _NAME.fullmatch(s) for s in secrets):
            raise ContractError("The application's Secret names required")
        if not others or not all(isinstance(o, ServiceAccount) for o in others):
            raise ContractError("The other workloads' service accounts required")
        self.reader, self.reviewer, self.as_account = reader, reviewer, as_account
        self.secrets, self.others = secrets, others

    def _positive(self):
        return [_outcome(self.reader.secret_status(name)) for name in self.secrets]

    def _attempts(self, account):
        client = self.as_account(account)
        found = {f"get:{name}": _outcome(client.secret_status(name)) for name in self.secrets}
        found["list:secrets"] = _outcome(client.list_status())
        found["create_pod_mounting_secret"] = _outcome(
            client.pod_dry_run_status(self.secrets[0]))
        return found

    def _review(self, account):
        review = self.reviewer.access_review
        found = {f"get:{name}": review(account, "get", "", "secrets", name)
                 for name in self.secrets}
        found["list:secrets"] = review(account, "list", "", "secrets")
        found["watch:secrets"] = review(account, "watch", "", "secrets")
        for group, resource in WORKLOADS:
            for verb in ("create", "update", "patch"):
                found[f"{verb}:{resource}"] = review(account, verb, group, resource)
        return found

    def cross_workload_secret_denied(self):
        before = self._positive()
        per_account = {}
        for account in self.others:
            attempts, review = self._attempts(account), self._review(account)
            per_account[account.user] = {
                "attempt": attempts,
                "review_allowed": sorted(k for k, allowed in review.items() if allowed)}
        after = self._positive()
        attempts = [a for p in per_account.values() for a in p["attempt"].values()]
        allowed = any(a == "allowed" for a in attempts) or any(
            p["review_allowed"] for p in per_account.values())
        if allowed:
            outcome = "allowed"
        elif all(p == "allowed" for p in before + after) and all(
                a == "refused" for a in attempts):
            outcome = "refused"
        else:
            outcome = "undecided"
        return outcome == "refused", {
            "probe": "cross_workload_secret_denied", "outcome": outcome,
            "secrets": list(self.secrets), "positive": {"before": before, "after": after},
            "principals": per_account}
