"""AWS Secrets Manager (the cloud_secrets secret store) to Kubernetes Secrets (on premises).

Run-scoped secrets with random values in the real AWS account move into
pre-created Secrets on a disposable kind cluster. The adapter's token may only
get and patch those Secrets. Values are compared in the test and never
printed; evidence must not contain them in any form.
"""

from datetime import UTC, datetime, timedelta
import json
import secrets

from aws_fixtures import assert_no_value_in, stored_secret
import pytest

from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.adapters.secrets_handoff import SecretsManagerToKubernetes
from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, ResourceScope

pytestmark = [pytest.mark.aws_live, pytest.mark.kubernetes]


def adapter(cloud_secrets, kind_secrets):
    return SecretsManagerToKubernetes(
        "secrets-handoff", secretsmanager=cloud_secrets["sm"], controls=kind_secrets["controls"](),
        mappings={cloud_secrets["names"]["db"]: ("orders-db", "credentials"),
                  cloud_secrets["names"]["api"]: ("orders-api", "api-key")})


def command(a):
    return EffectCommand(
        ResourceScope("tenant-orders", "orders", "kind_secrets-target", "unit-secrets"), "op-secrets",
        "copy", a.adapter_id, "copy_secrets", "op-secrets:copy", "a" * 64, "revision-1", 1,
        datetime.now(UTC) + timedelta(minutes=10), "{}")


def readback(a):
    return a.readback(command(a), datetime.now(UTC))


def test_secrets_move_and_no_value_ever_appears_in_evidence(cloud_secrets, kind_secrets):
    a = adapter(cloud_secrets, kind_secrets)
    before = readback(a)
    assert before.status == EffectStatus.UNKNOWN  # nothing copied yet
    a.validate(command(a))
    a.apply(command(a))
    after = readback(a)
    assert after.status == EffectStatus.APPLIED, after.evidence
    assert stored_secret(kind_secrets, "orders-db", "credentials") == cloud_secrets["values"]["db"]
    assert stored_secret(kind_secrets, "orders-api", "api-key") == cloud_secrets["values"]["api"]
    for observation in (before, after):
        assert_no_value_in(observation.evidence_json, cloud_secrets["values"].values())
    assert {s["target"] for s in after.evidence["secrets"]} == {
        "orders-db/credentials", "orders-api/api-key"}
    a.apply(command(a))  # idempotent
    assert readback(a).status == EffectStatus.APPLIED


def test_a_rotation_at_the_source_makes_the_copy_stale_until_copied_again(cloud_secrets, kind_secrets):
    a = adapter(cloud_secrets, kind_secrets)
    a.apply(command(a))
    rotated = json.dumps({"username": "orders", "password": secrets.token_urlsafe(24)})
    cloud_secrets["sm"].put_secret_value(SecretId=cloud_secrets["names"]["db"], SecretString=rotated)
    stale = readback(a)
    assert stale.status == EffectStatus.UNKNOWN
    assert {s["target"]: s["matches"] for s in stale.evidence["secrets"]} == {
        "orders-db/credentials": False, "orders-api/api-key": True}
    a.apply(command(a))
    assert readback(a).status == EffectStatus.APPLIED
    assert stored_secret(kind_secrets, "orders-db", "credentials") == rotated.encode()
    assert_no_value_in(stale.evidence_json, [rotated.encode(), *cloud_secrets["values"].values()])


def test_access_is_limited_to_the_registered_secrets_and_errors_say_nothing(cloud_secrets, kind_secrets):
    with pytest.raises(KubernetesRequestRefused) as refused:
        kind_secrets["controls"]().secret("unrelated")  # same namespace, not granted
    assert refused.value.status == 403
    missing = SecretsManagerToKubernetes(
        "secrets-handoff", secretsmanager=cloud_secrets["sm"], controls=kind_secrets["controls"](),
        mappings={f"{cloud_secrets['names']['db']}-missing": ("orders-db", "credentials")})
    with pytest.raises(ContractError) as error:
        missing.apply(command(missing))
    assert str(error.value) == f"Source secret unavailable: {cloud_secrets['names']['db']}-missing"
    assert error.value.__cause__ is None and error.value.__suppress_context__
