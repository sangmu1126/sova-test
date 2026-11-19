"""NET-01.3 for any unit whose data endpoint is a Service on a Kubernetes
target: ``deny_cross_namespace_network_access``.

An execution-file entry of any engine may name it::

    "network_reach": {"registration_file": "...",        # the application's namespace
                      "outside": [{"registration_file": "..."}, ...],  # other namespaces
                      "host": "orders-db.orders.svc", "port": 5432,
                      "client_labels": {"role": "orders-client"},
                      "image": "<python image>@sha256:<digest>", "run_as": [10001, 10001]}

Each registration may add ``token_file`` and ``ca_file`` (another cluster).
The worker's service account needs, in each named namespace, ``create``,
``get`` and ``delete`` of Pods and ``get`` of ``pods/log``: the check's
short-lived client Pods (``NetworkReachCheck``).
"""

from pathlib import Path

from sova.adapters.kubernetes.network_reach import NetworkReachCheck
from sova.adapters.kubernetes.registration import decode_registration
from sova.adapters.kubernetes.volume_controls import KubernetesVolumeControls
from sova.adapters.registration.graph_codec import json_document
from sova.domain.contracts import ContractError

CONTROL = "security.deny_cross_namespace_network_access"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_KEYS = {"registration_file", "token_file", "ca_file", "outside", "host", "port",
         "client_labels", "image", "run_as"}
_SIDE_KEYS = {"registration_file", "token_file", "ca_file"}


def _controls(spec, service_account):
    if type(spec) is not dict or set(spec) - _SIDE_KEYS or "registration_file" not in spec:
        raise ContractError("A namespace: registration_file (token_file, ca_file)")
    registration = decode_registration(json_document(
        Path(spec["registration_file"]).read_text()))
    ca = Path(spec.get("ca_file") or service_account / "ca.crt").read_bytes()
    token_file = Path(spec.get("token_file") or service_account / "token")
    return registration.namespace, KubernetesVolumeControls(
        registration, ca_pem=ca, token_provider=lambda: token_file.read_text().strip())


def network_reach_check(spec, *, service_account=SERVICE_ACCOUNT):
    if type(spec) is not dict or set(spec) - _KEYS or not _KEYS - {
            "token_file", "ca_file"} <= set(spec):
        raise ContractError("network_reach: registration_file, outside, host, port, "
                            "client_labels, image and run_as")
    if type(spec["outside"]) is not list or not spec["outside"]:
        raise ContractError("network_reach.outside: at least one namespace")
    if type(spec["run_as"]) is not list or len(spec["run_as"]) != 2:
        raise ContractError("network_reach.run_as: [user, group]")
    if "@sha256:" not in str(spec["image"]):
        raise ContractError("network_reach.image: pinned by digest")
    _, inside = _controls({k: spec[k] for k in _SIDE_KEYS if k in spec}, service_account)
    outside = [_controls(o, service_account) for o in spec["outside"]]
    return NetworkReachCheck(inside=inside, outside=outside, host=spec["host"],
                             port=spec["port"], client_labels=spec["client_labels"],
                             image=spec["image"], run_as=tuple(spec["run_as"]))
