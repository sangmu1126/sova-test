"""GCP services other than the database, measured in a real project.

* Secret Manager to Kubernetes Secrets on the local kind cluster.
* Cloud Run source compute stopped and read back, the old URL probed.
* Cloud DNS as the route store, switched and read from the zone's own
  authoritative servers.
* The Cloud Audit Logs change collector, waiting for real changes to reach
  the Admin Activity log.

Every resource is named ``sova-test-...-<run>`` and removed by its fixture;
``cloud-env.sh down`` removes whatever a stopped run left behind.
"""

from datetime import UTC, datetime, timedelta
import json
import secrets
import time

from aws_fixtures import assert_no_value_in, stored_secret
from gcp_fixtures import gcloud, run_answers
import pytest

from sova.adapters.gcp_audit_log_collector import GcpAuditLogCollector
from sova.adapters.gcp_cloud_dns_route import CloudDnsRoute
from sova.adapters.gcp_cloud_run_compute import CloudRunServiceCompute
from sova.adapters.gcp_secret_manager import SecretManagerToKubernetes
from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.adapters.postgres_offline_copy import PostgresOfflineCopyAdapter
from sova.domain.change_impact import ChangeKind
from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, ResourceScope

pytestmark = pytest.mark.gcp_live


def command(adapter, action, scope="gcp-source"):
    return EffectCommand(
        ResourceScope("tenant-orders", "orders", scope, "unit-" + action), "op-" + action,
        action, adapter.adapter_id, action, f"op-{action}:{action}", "a" * 64, "revision-1",
        1, datetime.now(UTC) + timedelta(minutes=10), "{}")


# -- Secret Manager -> Kubernetes Secrets -------------------------------------------------


def secrets_adapter(gcp_secrets, kind_secrets, mappings=None):
    return SecretManagerToKubernetes(
        "secrets-handoff", rest=gcp_secrets["rest"], controls=kind_secrets["controls"](),
        mappings=mappings or {gcp_secrets["names"]["db"]: ("orders-db", "credentials"),
                              gcp_secrets["names"]["api"]: ("orders-api", "api-key")})


@pytest.mark.kubernetes
def test_secret_manager_values_move_and_never_appear_in_evidence(gcp_secrets, kind_secrets):
    a = secrets_adapter(gcp_secrets, kind_secrets)
    cmd = command(a, "copy_secrets", "kind-target")
    before = a.readback(cmd, datetime.now(UTC))
    assert before.status == EffectStatus.UNKNOWN  # nothing copied yet
    a.validate(cmd)
    a.apply(cmd)
    after = a.readback(cmd, datetime.now(UTC))
    assert after.status == EffectStatus.APPLIED, after.evidence
    assert stored_secret(kind_secrets, "orders-db", "credentials") == gcp_secrets["values"]["db"]
    assert stored_secret(kind_secrets, "orders-api", "api-key") == gcp_secrets["values"]["api"]
    for observation in (before, after):
        assert_no_value_in(observation.evidence_json, gcp_secrets["values"].values())
    assert {s["source_version"] for s in after.evidence["secrets"]} == {"1"}


@pytest.mark.kubernetes
def test_a_new_version_at_the_source_makes_the_copy_stale_until_copied_again(gcp_secrets,
                                                                            kind_secrets):
    a = secrets_adapter(gcp_secrets, kind_secrets)
    cmd = command(a, "copy_secrets", "kind-target")
    a.apply(cmd)
    rotated = json.dumps({"username": "orders", "password": secrets.token_urlsafe(24)}).encode()
    gcp_secrets["add_version"]("db", rotated)
    stale = a.readback(cmd, datetime.now(UTC))
    assert stale.status == EffectStatus.UNKNOWN
    assert {s["target"]: (s["matches"], s["source_version"])
            for s in stale.evidence["secrets"]} == {
        "orders-db/credentials": (False, "2"), "orders-api/api-key": (True, "1")}
    a.apply(cmd)
    assert a.readback(cmd, datetime.now(UTC)).status == EffectStatus.APPLIED
    assert stored_secret(kind_secrets, "orders-db", "credentials") == rotated
    assert_no_value_in(stale.evidence_json, [rotated, *gcp_secrets["values"].values()])


@pytest.mark.kubernetes
def test_access_stays_limited_and_errors_say_nothing(gcp_secrets, kind_secrets):
    with pytest.raises(KubernetesRequestRefused) as refused:
        kind_secrets["controls"]().secret("unrelated")
    assert refused.value.status == 403
    missing = f"{gcp_secrets['names']['db']}-missing"
    a = secrets_adapter(gcp_secrets, kind_secrets, {missing: ("orders-db", "credentials")})
    with pytest.raises(ContractError) as error:
        a.apply(command(a, "copy_secrets", "kind-target"))
    assert str(error.value) == f"Source secret unavailable: {missing}"
    assert error.value.__cause__ is None and error.value.__suppress_context__


# -- Cloud Run source compute ------------------------------------------------------------


def test_cloud_run_source_compute_is_stopped_and_the_old_url_no_longer_answers(
        cloud_run_app, gcp_rest):
    app = cloud_run_app
    compute = CloudRunServiceCompute("cloud-run-source", rest=gcp_rest, region=app["region"],
                                     service=app["name"])
    urls = compute.endpoints()
    assert app["url"] in urls
    assert run_answers(app["url"], app["token"])  # the source application serves
    stopped, before = compute.observe()
    assert not stopped and before["scaling_mode"] == "AUTOMATIC"
    cmd = command(compute, "stop_source_compute")
    compute.validate(cmd)
    compute.apply(cmd)
    observation = compute.readback(cmd, datetime.now(UTC))
    assert observation.status == EffectStatus.APPLIED, observation.evidence
    deadline = time.monotonic() + 120
    while any(run_answers(url, app["token"]) for url in urls):
        assert time.monotonic() < deadline, "the stopped service still answers"
        time.sleep(5)
    compute.apply(cmd)  # idempotent
    assert compute.readback(cmd, datetime.now(UTC)).status == EffectStatus.APPLIED


def test_a_service_scaled_back_up_outside_the_plan_reads_unknown(cloud_run_app, gcp_rest):
    app = cloud_run_app
    compute = CloudRunServiceCompute("cloud-run-source", rest=gcp_rest, region=app["region"],
                                     service=app["name"])
    cmd = command(compute, "stop_source_compute")
    compute.apply(cmd)
    assert compute.readback(cmd, datetime.now(UTC)).status == EffectStatus.APPLIED
    gcloud({"project": gcp_rest.project}, "run", "services", "update", app["name"],
           "--region", app["region"], "--scaling=auto")  # someone starts it again
    assert compute.readback(cmd, datetime.now(UTC)).status == EffectStatus.UNKNOWN
    deadline = time.monotonic() + 120  # the first request after the change starts an instance
    while not run_answers(app["url"], app["token"]):
        assert time.monotonic() < deadline, "the service did not come back"
        time.sleep(5)


# -- Cloud DNS route store ---------------------------------------------------------------

SIDES = {"orders_src": "198.51.100.10", "orders_tgt": "203.0.113.20"}


def test_commit_route_switches_the_name_at_every_authoritative_server(dns_zone):
    zone = dns_zone
    record = "orders." + zone["name"]
    route = CloudDnsRoute(zone["rest"], zone=zone["zone"], name=record, sides=SIDES, ttl=60)
    route.set("orders_src")
    assert route.get() == "orders_src"
    adapter = PostgresOfflineCopyAdapter(
        "postgres-offline-copy", connect=lambda database: None, source_db="orders_src",
        target_db="orders_tgt", app_role="orders_app", tables=(("orders", "id"),),
        schema_sql="", route=route)
    cmd = command(adapter, "commit_route", "edge")
    adapter.validate(cmd)
    adapter.apply(cmd)
    assert adapter.readback(cmd, datetime.now(UTC)).status == EffectStatus.APPLIED
    observed = route.observe()
    assert observed["ttl"] == 60 and observed["change_done"]
    assert len(observed["servers"]) >= 2 and all(
        s["addresses"] == [SIDES["orders_tgt"]] and s["authoritative"]
        for s in observed["servers"].values()), observed["servers"]
    adapter.apply(cmd)  # idempotent
    assert adapter.readback(cmd, datetime.now(UTC)).status == EffectStatus.APPLIED


def test_an_edit_in_between_is_refused_and_an_outside_change_reads_unknown(dns_zone):
    zone = dns_zone
    record = "orders." + zone["name"]
    route = CloudDnsRoute(zone["rest"], zone=zone["zone"], name=record, sides=SIDES, ttl=60)
    route.set("orders_src")
    stale = route._record()
    url = (f"https://dns.googleapis.com/dns/v1/projects/{zone['rest'].project}/managedZones/"
           f"{zone['zone']}/changes")
    zone["rest"].post(url, {"deletions": [stale], "additions": [
        {**{k: stale[k] for k in ("name", "type", "ttl")}, "rrdatas": ["192.0.2.99"]}]})
    deadline = time.monotonic() + 60
    while route.get() is not None or route.observe()["answer"] != ["192.0.2.99"]:
        assert time.monotonic() < deadline
        time.sleep(2)  # neither side: the name serves something else
    route._record = lambda: stale  # a switch prepared from the earlier read
    with pytest.raises(ContractError, match="changed in between"):
        route.set("orders_tgt")


# -- Cloud Audit Logs change collector ---------------------------------------------------


class RecordingFeed:
    def __init__(self):
        self.recorded, self.advanced = [], []

    def record(self, event, *, collector_id):
        self.recorded.append(event)

    def advance(self, environment, *, collector_id, observed_through):
        self.advanced.append(observed_through)


def test_the_audit_log_collector_records_real_policy_and_version_changes(gcp, gcp_rest,
                                                                         gcp_secrets):
    number = gcloud(gcp, "projects", "describe", gcp["project"])["projectNumber"]
    name = gcp_secrets["names"]["db"]
    resource = f"projects/{number}/secrets/{name}"
    url = f"https://secretmanager.googleapis.com/v1/projects/{gcp['project']}/secrets/{name}"
    changed = datetime.now(UTC)
    policy = gcp_rest.get(url + ":getIamPolicy")
    member = f"serviceAccount:sova-test-expiry-{gcp['run']}@{gcp['project']}.iam.gserviceaccount.com"
    gcp_rest.post(url + ":setIamPolicy", {"policy": {**policy, "bindings": [
        *policy.get("bindings", []),
        {"role": "roles/secretmanager.secretVersionManager", "members": [member]}]}})
    gcp_secrets["add_version"]("db", secrets.token_bytes(16))  # a rotation
    feed = RecordingFeed()
    collector = GcpAuditLogCollector(
        feed, rest=gcp_rest, clock=lambda: datetime.now(UTC), tenant_id="tenant-a",
        application_id="orders", collector_id="audit-live",
        resources={resource: ("gcp-source", "uid-orders-db-secret")},
        delivery_lag=timedelta(minutes=10), lookback=timedelta(minutes=30))
    found, deadline = [], time.monotonic() + 10 * 60
    while time.monotonic() < deadline:
        found += collector.poll()
        polled_at = datetime.now(UTC)
        kinds = [e.kind for e in found]
        if ChangeKind.POLICY in kinds and kinds.count(ChangeKind.RESOURCE) >= 3:
            break  # created, its first version, the rotation
        time.sleep(15)
    policy_events = [e for e in found if e.kind == ChangeKind.POLICY]
    assert policy_events and policy_events[0].subject == "uid-orders-db-secret"
    assert abs(policy_events[0].observed_at - changed) < timedelta(minutes=2)
    assert [e.kind for e in found].count(ChangeKind.RESOURCE) >= 3, found
    assert len({e.event_id for e in feed.recorded}) == len(feed.recorded)  # once each
    assert feed.advanced == []  # observed delivery is not exhaustive coverage
    print(f"policy change visible after {(polled_at - changed).total_seconds():.0f}s")
