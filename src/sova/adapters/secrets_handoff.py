"""Secret handoff: a cloud secret store to Kubernetes Secrets (on premises).

The source is any store that answers ``current(secret_id)`` with the current
version and value: AWS Secrets Manager here (``AWSCURRENT``), GCP Secret
Manager in ``gcp_secret_manager``.

Each registered mapping names a source secret and a target Kubernetes Secret
with a data key. The target Secret is created beforehand by the environment's
owner, so the adapter only ever changes values of objects it was given
(least privilege: ``get`` and ``patch`` on those names).

* ``copy_secrets`` reads each source secret's ``AWSCURRENT`` value and writes it
  into the target key with a JSON Patch that tests the Secret's
  ``resourceVersion`` first.
* The readback reads both sides again and holds only if every target value
  equals the source's current value. A rotation at the source after the copy
  therefore reads back as UNKNOWN: the target would serve a stale credential.

Values exist only in memory during a call. Observations and errors carry the
secret names, the source version IDs and whether each value matches - never
a value, an encoding of it, or a digest of it (a digest of a short secret can
be brute-forced). Provider error messages are not passed on.

Revoking or rotating the credential behind a secret, so that the copy left in
the cloud stops working, is a separate approved step; this adapter does not
claim it: a readable source is still a dependency.
"""

import base64
import re

from botocore.exceptions import ClientError

from sova.adapters.kubernetes.handoff_controls import KubernetesHandoffControls
from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"copy_secrets"})
_SOURCE = re.compile(r"[A-Za-z0-9/_+=.@-]{1,512}")


def _value(response):
    if "SecretString" in response:
        return response["SecretString"].encode()
    if "SecretBinary" in response:
        return bytes(response["SecretBinary"])
    raise ContractError("Secret without a value")


class AwsSecretsManagerSource:
    def __init__(self, client):
        if not callable(getattr(client, "get_secret_value", None)):
            raise ContractError("Secrets Manager client required")
        self.client = client

    def current(self, secret_id):
        try:
            response = self.client.get_secret_value(SecretId=secret_id,
                                                    VersionStage="AWSCURRENT")
        except ClientError:  # the provider's message is not passed on
            raise ContractError(f"Source secret unavailable: {secret_id}") from None
        return response["VersionId"], _value(response)


class SecretsToKubernetes:
    def __init__(self, adapter_id, *, source, controls, mappings):
        """``mappings``: source secret ID -> (target Secret name, data key)."""
        if not callable(getattr(source, "current", None)):
            raise ContractError("Secret source required")
        if not isinstance(controls, KubernetesHandoffControls):
            raise ContractError("Registered Kubernetes controls required")
        if type(mappings) is not dict or not 1 <= len(mappings) <= 256:
            raise ContractError("Bounded secret mappings required")
        targets = set()
        for secret_id, target in mappings.items():
            if type(secret_id) is not str or not _SOURCE.fullmatch(secret_id):
                raise ContractError("Source secret ID required")
            if type(target) is not tuple or len(target) != 2 or target in targets:
                raise ContractError("Distinct (Secret, key) targets required")
            targets.add(target)
        self.adapter_id, self.source, self.controls = adapter_id, source, controls
        self.mappings = dict(sorted(mappings.items()))

    def validate(self, command):
        if command.adapter_id != self.adapter_id or command.action not in ACTIONS:
            raise ContractError("Unsupported secret action for this adapter")

    def _source(self, secret_id):
        return self.source.current(secret_id)

    def apply(self, command):
        for secret_id, (name, key) in self.mappings.items():
            _, value = self._source(secret_id)
            current = self.controls.secret(name)
            self.controls.set_secret_value(name, current["metadata"]["resourceVersion"], key,
                                           base64.b64encode(value).decode())
            del value

    def readback(self, command, now):
        results = []
        for secret_id, (name, key) in self.mappings.items():
            version, value = self._source(secret_id)
            data = (self.controls.secret(name).get("data") or {}).get(key)
            try:
                matches = data is not None and base64.b64decode(data, validate=True) == value
            except ValueError:
                matches = False
            del value, data
            results.append({"source": secret_id, "source_version": version,
                            "target": f"{name}/{key}", "matches": matches})
        holds = all(r["matches"] for r in results)
        return EffectObservation(
            command.scope, command.fingerprint,
            EffectStatus.APPLIED if holds else EffectStatus.UNKNOWN, now,
            canonical_json({"action": command.action, "secrets": results}, limit=65536))


class SecretsManagerToKubernetes(SecretsToKubernetes):
    """AWS Secrets Manager to Kubernetes Secrets."""

    def __init__(self, adapter_id, *, secretsmanager, controls, mappings):
        super().__init__(adapter_id, source=AwsSecretsManagerSource(secretsmanager),
                         controls=controls, mappings=mappings)

    @property
    def sm(self):
        return self.source.client
