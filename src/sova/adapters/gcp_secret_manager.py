"""Secret handoff from GCP Secret Manager to Kubernetes Secrets (on premises).

The source answers with the secret's ``latest`` version: the newest enabled
one, which is what an application reading ``latest`` gets. The payload's
CRC32C checksum, when Secret Manager sends one, is checked before the value
is used, so a corrupted read never reaches the target. The rest - patching
only pre-created Secrets, readback against the source's current value, no
value or digest in any observation - is shared with the AWS source
(``secrets_handoff``).

Disabling or destroying the source versions, so that the copy left in the
cloud stops working, is a separate approved step.
"""

import base64
import re

from sova.adapters.gcp_rest import GcpApiError, GcpRest
from sova.adapters.secrets_handoff import SecretsToKubernetes
from sova.domain.contracts import ContractError

API = "https://secretmanager.googleapis.com/v1"
_SECRET = re.compile(r"[A-Za-z0-9_-]{1,255}")


def _crc32c_table():
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_TABLE = _crc32c_table()


def crc32c(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


class GcpSecretManagerSource:
    def __init__(self, rest):
        if not isinstance(rest, GcpRest):
            raise ContractError("GCP REST access required")
        self.rest = rest

    def current(self, secret_id):
        if type(secret_id) is not str or not _SECRET.fullmatch(secret_id):
            raise ContractError("Secret Manager secret ID required")
        try:
            response = self.rest.get(f"{API}/projects/{self.rest.project}/secrets/{secret_id}"
                                     "/versions/latest:access")
        except GcpApiError:  # the provider's message is not passed on
            raise ContractError(f"Source secret unavailable: {secret_id}") from None
        payload = response.get("payload") or {}
        if "data" not in payload:
            raise ContractError("Secret without a value")
        value = base64.b64decode(payload["data"])
        expected = payload.get("dataCrc32c")
        if expected is not None and crc32c(value) != int(expected):
            del value
            raise ContractError(f"Source secret failed its checksum: {secret_id}")
        version = response["name"].rsplit("/", 1)[-1]
        return version, value


class SecretManagerToKubernetes(SecretsToKubernetes):
    """GCP Secret Manager to Kubernetes Secrets."""

    def __init__(self, adapter_id, *, rest, controls, mappings):
        super().__init__(adapter_id, source=GcpSecretManagerSource(rest), controls=controls,
                         mappings=mappings)
