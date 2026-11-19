"""SEC-01.1 for any unit whose application keeps Secrets on a Kubernetes
target: ``deny_cross_workload_secret_access``.

An execution-file entry of any engine may name them::

    "secret_access": {"registration_file": "...",      # the target namespace
                      "token_file": "...", "ca_file": "...",   # another cluster
                      "secrets": ["orders-db"],
                      "others": [["orders", "billing"], ["reports", "reader"]]}

``others`` are the service accounts of the other workloads to measure, here
or in other namespaces. The worker reaches the namespace with its own
service account, which needs ``get`` on the named Secrets, ``create`` on
``subjectaccessreviews`` and ``create`` on ``serviceaccounts/token`` for
exactly the named accounts: each one's attempts are made with a token issued
for ten minutes and kept in memory (``KubernetesSecretProbes``).
"""

from pathlib import Path

from sova.adapters.kubernetes.registration import decode_registration
from sova.adapters.kubernetes_secret_probes import (
    KubernetesSecretProbes,
    SecretAccessControls,
    ServiceAccount,
)
from sova.adapters.registration.graph_codec import json_document
from sova.domain.contracts import ContractError
from sova.service.worker import Execution

CONTROL = "security.deny_cross_workload_secret_access"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_KEYS = {"registration_file", "token_file", "ca_file", "secrets", "others"}


def secret_access_probes(spec, *, service_account=SERVICE_ACCOUNT):
    if type(spec) is not dict or set(spec) - _KEYS or not {
            "registration_file", "secrets", "others"} <= set(spec):
        raise ContractError("secret_access: registration_file, secrets and others")
    others = spec["others"]
    if type(others) is not list or not others or not all(
            type(o) is list and len(o) == 2 for o in others):
        raise ContractError("secret_access.others: [[namespace, service account], ...]")
    registration = decode_registration(json_document(
        Path(spec["registration_file"]).read_text()))
    ca = Path(spec.get("ca_file") or service_account / "ca.crt").read_bytes()
    token_file = Path(spec.get("token_file") or service_account / "token")
    return probes_from(SecretAccessControls(
        registration, ca_pem=ca, token_provider=lambda: token_file.read_text().strip()),
        secrets=spec["secrets"], others=[ServiceAccount(*o) for o in others])


def probes_from(platform, *, secrets, others):
    """The probes, each other account acting with a token the platform has
    issued for it."""
    tokens = {}

    def as_account(account):
        def token():
            if account not in tokens:
                tokens[account] = platform.token_for(account)
            return tokens[account]
        return SecretAccessControls(platform.registration, ca_pem=platform.ca_pem,
                                    token_provider=token)

    return KubernetesSecretProbes(reader=platform, reviewer=platform, as_account=as_account,
                                  secrets=secrets, others=others)


def with_target_controls(factory, controls):
    """``factory`` with each obligation of ``controls`` (``{obligation id:
    () -> (passed, observation)}``) measured by it; every other obligation is
    the unit's own."""
    def build(*args):
        execution = factory(*args)

        def measure(obligation, times):
            found = controls.get(obligation["id"])
            return found() if found else execution.measure(obligation, times)
        return Execution(execution.adapter, measure, close=execution.close)
    return build


def with_secret_access(factory, probes):
    """``factory`` with ``deny_cross_workload_secret_access`` measured by
    ``probes``."""
    return with_target_controls(factory, {CONTROL: probes.cross_workload_secret_denied})
