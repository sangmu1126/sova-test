"""AWS Secrets Manager (the cloud secret store) to Kubernetes Secrets (on premises).

Run-scoped secrets with random values in the real AWS account move into
pre-created Secrets on a disposable kind cluster. The adapter's token may only
get and patch those Secrets. Values are compared in the test and never
printed; evidence must not contain them in any form.
"""

import base64
from datetime import UTC, datetime, timedelta
import json
import secrets
from uuid import uuid4

from kind_support import delete_namespace, kind_namespace, kubectl
import pytest

from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.adapters.secrets_handoff import SecretsManagerToKubernetes
from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, ResourceScope

pytestmark = [pytest.mark.aws_live, pytest.mark.kubernetes]
TARGETS = ("orders-db", "orders-api")


@pytest.fixture(scope="module")
def onprem():
    rules = [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": list(TARGETS),
              "verbs": ["get", "patch"]}]
    objects = [{"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name}} for name in TARGETS]
    objects.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                    "metadata": {"name": "unrelated"},
                    "data": {"token": base64.b64encode(b"not-for-the-handoff").decode()}})
    context, namespace, controls = kind_namespace(rules, objects, prefix="secrets")
    yield {"context": context, "namespace": namespace, "controls": controls}
    delete_namespace(context, namespace)


@pytest.fixture
def cloud(aws):
    sm = aws["session"].client("secretsmanager")
    base = f"sova-test/{aws['run']}/{uuid4().hex[:6]}"
    values = {"db": json.dumps({"username": "orders",
                                "password": secrets.token_urlsafe(24)}).encode(),
              "api": secrets.token_bytes(32)}
    names = {"db": f"{base}/orders-db", "api": f"{base}/orders-api-key"}
    created = [sm.create_secret(Name=names["db"], SecretString=values["db"].decode(),
                                Tags=aws["tags"])["ARN"],
               sm.create_secret(Name=names["api"], SecretBinary=values["api"],
                                Tags=aws["tags"])["ARN"]]
    yield {"sm": sm, "names": names, "values": values}
    for arn in created:
        sm.delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)


def adapter(cloud, onprem):
    return SecretsManagerToKubernetes(
        "secrets-handoff", secretsmanager=cloud["sm"], controls=onprem["controls"](),
        mappings={cloud["names"]["db"]: ("orders-db", "credentials"),
                  cloud["names"]["api"]: ("orders-api", "api-key")})


def command(a):
    return EffectCommand(
        ResourceScope("tenant-orders", "orders", "onprem-target", "unit-secrets"), "op-secrets",
        "copy", a.adapter_id, "copy_secrets", "op-secrets:copy", "a" * 64, "revision-1", 1,
        datetime.now(UTC) + timedelta(minutes=10), "{}")


def readback(a):
    return a.readback(command(a), datetime.now(UTC))


def stored(onprem, name, key):
    encoded = kubectl(onprem["context"], "-n", onprem["namespace"], "get", "secret", name,
                      "-o", f"jsonpath={{.data.{key}}}").strip()
    return base64.b64decode(encoded) if encoded else None


def assert_no_value_in(text, values):
    for value in values:
        forms = {value, base64.b64encode(value)}
        if value.startswith(b"{"):
            forms.add(json.loads(value)["password"].encode())
        assert not any(form in text.encode() for form in forms), "a secret value leaked"


def test_secrets_move_and_no_value_ever_appears_in_evidence(cloud, onprem):
    a = adapter(cloud, onprem)
    before = readback(a)
    assert before.status == EffectStatus.UNKNOWN  # nothing copied yet
    a.validate(command(a))
    a.apply(command(a))
    after = readback(a)
    assert after.status == EffectStatus.APPLIED, after.evidence
    assert stored(onprem, "orders-db", "credentials") == cloud["values"]["db"]
    assert stored(onprem, "orders-api", "api-key") == cloud["values"]["api"]
    for observation in (before, after):
        assert_no_value_in(observation.evidence_json, cloud["values"].values())
    assert {s["target"] for s in after.evidence["secrets"]} == {
        "orders-db/credentials", "orders-api/api-key"}
    a.apply(command(a))  # idempotent
    assert readback(a).status == EffectStatus.APPLIED


def test_a_rotation_at_the_source_makes_the_copy_stale_until_copied_again(cloud, onprem):
    a = adapter(cloud, onprem)
    a.apply(command(a))
    rotated = json.dumps({"username": "orders", "password": secrets.token_urlsafe(24)})
    cloud["sm"].put_secret_value(SecretId=cloud["names"]["db"], SecretString=rotated)
    stale = readback(a)
    assert stale.status == EffectStatus.UNKNOWN
    assert {s["target"]: s["matches"] for s in stale.evidence["secrets"]} == {
        "orders-db/credentials": False, "orders-api/api-key": True}
    a.apply(command(a))
    assert readback(a).status == EffectStatus.APPLIED
    assert stored(onprem, "orders-db", "credentials") == rotated.encode()
    assert_no_value_in(stale.evidence_json, [rotated.encode(), *cloud["values"].values()])


def test_access_is_limited_to_the_registered_secrets_and_errors_say_nothing(cloud, onprem):
    with pytest.raises(KubernetesRequestRefused) as refused:
        onprem["controls"]().secret("unrelated")  # same namespace, not granted
    assert refused.value.status == 403
    missing = SecretsManagerToKubernetes(
        "secrets-handoff", secretsmanager=cloud["sm"], controls=onprem["controls"](),
        mappings={f"{cloud['names']['db']}-missing": ("orders-db", "credentials")})
    with pytest.raises(ContractError) as error:
        missing.apply(command(missing))
    assert str(error.value) == f"Source secret unavailable: {cloud['names']['db']}-missing"
    assert error.value.__cause__ is None and error.value.__suppress_context__
