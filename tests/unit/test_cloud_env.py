"""The cloud switch (infra/cloud-env): on and off for AWS and GCP together.

The rules that keep it safe are tested here without any cloud: only names of
this run are touched, a failed listing is never read as "nothing left", the
lease exists before any resource does, the lease ends only when both clouds
are empty, and no secret value is ever printed.
"""

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "cloud_env", Path(__file__).resolve().parents[2] / "infra/cloud-env/cloud_env.py")
cloud_env = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cloud_env)

RUN = "2b3e0d"
T0 = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize("name", [
    f"sova-test-src-{RUN}-abc123", f"sova-test/{RUN}/x1/orders-db", f"sova-test-{RUN}-a1b2c3.com.",
    f"sova-test-pg-{RUN}", f"sova-test-expiry-{RUN}", f"sova-test-pool-{RUN}-ff00aa"])
def test_names_of_this_run_are_in_scope(name):
    assert cloud_env.scoped(name, RUN)


@pytest.mark.parametrize("name", [
    "sova-test-src-2b3e0e-abc123",  # another run
    f"orders-tgt-{RUN}",  # not a sova-test name
    f"sova-test-src-x{RUN}-abc",  # the run id only as part of a token
    "sky-personal-db", "sova-response-hy1112a-audit", f"prod-sova-test-{RUN}",
    "sova-test-src-abc",
])
def test_everything_else_is_out_of_scope(name):
    assert not cloud_env.scoped(name, RUN)


def test_a_malformed_run_id_scopes_nothing():
    assert not cloud_env.scoped("sova-test-src-x-1", "x")
    assert not cloud_env.scoped("sova-test-src--1", "")


class Clock:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at


class FakeProvider:
    def __init__(self, name, *, fail_up=False, leftovers=()):
        self.name, self.fail_up = name, fail_up
        self.resources, self.leftovers, self.calls = [], list(leftovers), []

    def up(self, lease):
        self.calls.append("up")
        self.resources.append(("database", f"sova-test-pg-{lease['run']}"))
        if self.fail_up:
            raise cloud_env.CloudEnvError("quota exceeded")
        return {"instance": f"sova-test-pg-{lease['run']}"}

    def inventory(self, run):
        return list(self.resources)

    def sweep(self, run, log):
        self.calls.append("sweep")
        self.resources = list(self.leftovers)
        return self.inventory(run)

    def extend(self, lease, expires):
        self.calls.append(("extend", expires))


class FakeAgent:
    def __init__(self):
        self.installed = False

    def install(self):
        self.installed = True

    def remove(self):
        self.installed = False


def switch(tmp_path, providers, clock=None):
    out = []
    s = cloud_env.Switch(cloud_env.Paths(tmp_path / "cloud"), providers=providers,
                         clock=clock or Clock(), out=out.append, agent=FakeAgent(), ip="198.51.100.7")
    return s, out


def test_up_then_down_leaves_nothing_and_ends_the_lease(tmp_path):
    aws, gcp = FakeProvider("aws"), FakeProvider("gcp")
    s, out = switch(tmp_path, {"aws": aws, "gcp": gcp})
    assert s.up(8, ["aws", "gcp"]) == 0
    lease = s.lease()
    assert lease["state"] == "up" and lease["expires_at"] == "2026-11-15T16:00:00Z"
    assert s.agent.installed
    assert s.down() == 0
    assert s.lease() is None and not s.agent.installed
    archived = json.loads((tmp_path / "cloud/history" / f"{lease['run']}.json").read_text())
    assert archived["state"] == "down" and archived["reason"] == "requested"


def test_the_lease_exists_before_any_resource(tmp_path):
    """A failed or interrupted up can always be swept: the run id is on disk
    before the first resource is created."""
    seen = {}

    class Recording(FakeProvider):
        def up(self, lease):
            seen["lease"] = json.loads((tmp_path / "cloud/lease.json").read_text())
            return super().up(lease)

    s, _ = switch(tmp_path, {"aws": Recording("aws")})
    s.up(8, ["aws"])
    assert seen["lease"]["state"] == "starting" and seen["lease"]["run"] == s.lease()["run"]


def test_one_cloud_failing_keeps_the_lease_for_down(tmp_path):
    aws, gcp = FakeProvider("aws"), FakeProvider("gcp", fail_up=True)
    s, out = switch(tmp_path, {"aws": aws, "gcp": gcp})
    assert s.up(8, ["aws", "gcp"]) == 1
    assert s.lease()["state"] == "partial"
    assert "quota exceeded" in s.lease()["clouds"]["gcp"]["error"]
    assert s.down() == 0  # the half-created gcp database is swept too
    assert gcp.calls == ["up", "sweep"]


def test_up_again_resumes_only_the_failed_cloud_in_the_same_run(tmp_path):
    aws, gcp = FakeProvider("aws"), FakeProvider("gcp", fail_up=True)
    s, out = switch(tmp_path, {"aws": aws, "gcp": gcp})
    s.up(8, ["aws", "gcp"])
    first = s.lease()
    gcp.fail_up = False
    assert s.up(8, ["aws", "gcp"]) == 0
    lease = s.lease()
    assert lease["run"] == first["run"] and lease["expires_at"] == first["expires_at"]
    assert lease["state"] == "up" and aws.calls == ["up"] and gcp.calls == ["up", "up"]


def test_a_second_up_is_refused_while_a_run_is_on(tmp_path):
    s, _ = switch(tmp_path, {"aws": FakeProvider("aws")})
    s.up(8, ["aws"])
    with pytest.raises(cloud_env.CloudEnvError, match="extend, or down first"):
        s.up(8, ["aws"])


@pytest.mark.parametrize("hours", [0, -1, 25])
def test_a_lease_is_bounded(tmp_path, hours):
    s, _ = switch(tmp_path, {"aws": FakeProvider("aws")})
    with pytest.raises(cloud_env.CloudEnvError):
        s.up(hours, ["aws"])
    assert s.lease() is None


def test_leftovers_keep_the_lease_open(tmp_path):
    gcp = FakeProvider("gcp", leftovers=[("bucket", f"sova-test-witness-{RUN}-x")])
    s, out = switch(tmp_path, {"aws": FakeProvider("aws"), "gcp": gcp})
    s.up(8, ["aws", "gcp"])
    assert s.down() == 2
    assert s.lease()["state"] == "stopping" and s.agent.installed
    assert any("still there - bucket" in line for line in out)


def test_a_failed_listing_is_not_empty(tmp_path):
    class Broken(FakeProvider):
        def sweep(self, run, log):
            raise cloud_env.CloudEnvError("token expired")

    s, out = switch(tmp_path, {"aws": Broken("aws")})
    s.up(8, ["aws"])
    assert s.down() == 2
    assert s.lease() is not None
    assert any("token expired" in line for line in out)


def test_expire_acts_only_after_the_end(tmp_path):
    clock = Clock()
    aws = FakeProvider("aws")
    s, _ = switch(tmp_path, {"aws": aws}, clock)
    s.up(8, ["aws"])
    clock.at = T0 + timedelta(hours=7, minutes=59)
    assert s.expire() == 0 and s.lease() is not None and "sweep" not in aws.calls
    clock.at = T0 + timedelta(hours=8)
    assert s.expire() == 0 and s.lease() is None
    archived = next((tmp_path / "cloud/history").glob("*.json"))
    assert json.loads(archived.read_text())["reason"] == "expired"


def test_extend_moves_the_end_and_each_cloud_backstop(tmp_path):
    clock = Clock()
    aws, gcp = FakeProvider("aws"), FakeProvider("gcp")
    s, _ = switch(tmp_path, {"aws": aws, "gcp": gcp}, clock)
    s.up(8, ["aws", "gcp"])
    s.extend(2)
    assert s.lease()["expires_at"] == "2026-11-15T18:00:00Z"
    assert ("extend", T0 + timedelta(hours=10)) in aws.calls
    assert ("extend", T0 + timedelta(hours=10)) in gcp.calls
    with pytest.raises(cloud_env.CloudEnvError, match="at most"):
        s.extend(20)


def test_the_printed_environment_holds_no_secret(tmp_path):
    paths = cloud_env.Paths(tmp_path / "cloud")
    paths.ensure()
    values = {}
    for name in ("rds-master", "rds-role", "cloudsql-root", "cloudsql-role"):
        paths.new_secret(name)
        values[name] = paths.read_secret(name)
    lease = {"run": RUN, "state": "up", "clouds": {
        "aws": {"host": f"sova-test-pg-{RUN}.abc.ap-northeast-2.rds.amazonaws.com",
                "master": "sova_admin", "rds_id": f"sova-test-pg-{RUN}", "ca": "/ca/rds.pem"},
        "gcp": {"instance": f"sova-test-pg-{RUN}", "ip": "203.0.113.9",
                "dns_name": "abc.asia-northeast3.sql.goog", "ca": "/ca/sql.pem"}}}
    text = cloud_env.render_env(lease, paths, {"aws": {"profile": "p", "region": "r"},
                                               "gcp": {"project": "q", "region": "s"}})
    for value in values.values():
        assert value not in text
    assert "passfile=" in text and '"$(cat ' in text
    assert "host=abc.asia-northeast3.sql.goog hostaddr=203.0.113.9" in text
    assert "sslmode=verify-full" in text
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in paths.secrets.iterdir())


def _runner(responses):
    """A fake command line: the first matching prefix answers; calls are kept."""
    calls = []

    def run(args, *, input=None, check=True, timeout=None):
        calls.append(args)
        for prefix, payload in responses:
            if args[:len(prefix)] == prefix:
                if isinstance(payload, Exception):
                    if check:
                        raise payload
                    return SimpleNamespace(returncode=1, stdout="", stderr=str(payload))
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run, calls


def test_the_aws_sweep_deletes_only_this_runs_resources():
    run, calls = _runner([
        (["aws", "s3api", "list-buckets"], {"Buckets": [
            {"Name": f"sova-test-src-{RUN}-aa"}, {"Name": "sova-test-src-111111-aa"},
            {"Name": "sova-response-hy1112a-audit"}]}),
        (["aws", "rds", "describe-db-instances"], {"DBInstances": [
            {"DBInstanceIdentifier": f"sova-test-pg-{RUN}"},
            {"DBInstanceIdentifier": "sky-personal-db"}]}),
        (["aws", "iam", "list-roles"], {"Roles": [
            {"RoleName": f"sova-test-app-{RUN}"}, {"RoleName": "admin-role"}]}),
        (["aws", "iam", "list-role-policies"], {"PolicyNames": []}),
        (["aws", "iam", "list-attached-role-policies"], {"AttachedPolicies": []}),
        (["aws", "s3api", "list-object-versions"], {}),
        (["aws", "s3api", "list-multipart-uploads"], {}),
        (["aws", "s3api", "get-object-lock-configuration"], cloud_env.CloudEnvError("none")),
    ])
    aws = cloud_env.Aws({"profile": "p", "region": "r"}, run=run)
    aws.sweep(RUN, lambda line: None)
    deletes = [c for c in calls if any(w.startswith("delete") for w in c[1:3])]
    targets = " ".join(" ".join(c) for c in deletes)
    assert f"sova-test-src-{RUN}-aa" in targets and f"sova-test-pg-{RUN}" in targets
    assert f"sova-test-app-{RUN}" in targets
    for other in ("sova-test-src-111111-aa", "sova-response-hy1112a-audit", "sky-personal-db",
                  "admin-role"):
        assert other not in targets
    assert all("--profile" in c and "p" in c for c in calls)


def test_the_gcp_sweep_deletes_only_this_runs_resources():
    email = f"sova-test-expiry-{RUN}@q.iam.gserviceaccount.com"
    run, calls = _runner([
        (["gcloud", "services", "list"], [{"config": {"name": n}} for n in (
            "sqladmin.googleapis.com", "storage.googleapis.com", "iam.googleapis.com",
            "cloudscheduler.googleapis.com")]),
        (["gcloud", "sql", "instances", "list"], [{"name": f"sova-test-pg-{RUN}"},
                                                  {"name": "prod-db"}]),
        (["gcloud", "storage", "buckets", "list"], [{"name": f"sova-test-src-{RUN}-x"},
                                                    {"name": "q.appspot.com"}]),
        (["gcloud", "scheduler", "jobs", "list"], [
            {"name": f"projects/q/locations/r/jobs/sova-test-expiry-{RUN}"},
            {"name": "projects/q/locations/r/jobs/nightly"}]),
        (["gcloud", "iam", "service-accounts", "list"], [{"email": email},
                                                         {"email": "ci@q.iam.gserviceaccount.com"}]),
        (["gcloud", "projects", "get-iam-policy"], {"bindings": [
            {"role": "roles/cloudsql.admin", "members": [f"serviceAccount:{email}"]},
            {"role": "roles/owner", "members": ["user:someone@example.test",
                                                "serviceAccount:ci@q.iam.gserviceaccount.com"]}]}),
        (["gcloud", "sql", "instances", "delete"], {"name": "op-1"}),
    ])
    gcp = cloud_env.Gcp({"project": "q", "region": "r"}, run=run,
                        http=lambda *a, **k: pytest.fail("no REST call expected"))
    gcp.sweep(RUN, lambda line: None)
    deleting = [c for c in calls if "delete" in c or "rm" in c or "remove-iam-policy-binding" in c]
    text = " ".join(" ".join(c) for c in deleting)
    assert f"sova-test-pg-{RUN}" in text and f"gs://sova-test-src-{RUN}-x" in text
    assert email in text and f"sova-test-expiry-{RUN}" in text
    for other in ("prod-db", "q.appspot.com", "nightly", "ci@q", "someone@example.test"):
        assert other not in text
    assert all("--project" in c and "q" in c for c in calls if c[0] == "gcloud"
               and c[1:3] != ["auth", "print-access-token"])


def test_clusters_of_this_run_go_node_group_first_and_others_stay():
    run, calls = _runner([
        (["aws", "eks", "list-clusters"], {"clusters": [f"sova-test-k8s-{RUN}", "team-prod"]}),
        (["aws", "eks", "list-nodegroups"], {"nodegroups": [f"sova-test-ng-{RUN}"]}),
    ])
    cloud_env.Aws({"profile": "p", "region": "r"}, run=run).sweep(RUN, lambda line: None)
    deletes = [c[2] + " " + " ".join(c[3:7]) for c in calls
               if c[:2] == ["aws", "eks"] and c[2].startswith("delete")]
    assert deletes[0].startswith("delete-nodegroup") and f"sova-test-ng-{RUN}" in deletes[0]
    assert deletes[1].startswith("delete-cluster") and f"sova-test-k8s-{RUN}" in deletes[1]
    assert not any("team-prod" in " ".join(c) for c in calls if "delete" in " ".join(c[:3]))
    waits = [c[2:4] for c in calls if c[:3] == ["aws", "eks", "wait"]]
    assert waits == [["wait", "nodegroup-deleted"], ["wait", "cluster-deleted"]]


def test_gke_clusters_of_this_run_are_found_and_deleted():
    run, calls = _runner([
        (["gcloud", "services", "list"], [{"config": {"name": "container.googleapis.com"}}]),
        (["gcloud", "container", "clusters", "list"], [
            {"name": f"sova-test-k8s-{RUN}", "location": "r"},
            {"name": "analytics", "location": "r"}]),
    ])
    gcp = cloud_env.Gcp({"project": "q", "region": "r"}, run=run,
                        http=lambda *a, **k: pytest.fail("no REST call expected"))
    assert ("gke-cluster", f"sova-test-k8s-{RUN}@r") in gcp.inventory(RUN)
    gcp.sweep(RUN, lambda line: None)
    deleted = [c for c in calls if c[1:4] == ["container", "clusters", "delete"]]
    assert [c[4] for c in deleted] == [f"sova-test-k8s-{RUN}"]


def test_the_printed_environment_names_the_managed_clusters_without_credentials(tmp_path):
    paths = cloud_env.Paths(tmp_path / "cloud")
    paths.ensure()
    lease = {"run": RUN, "state": "up", "clouds": {
        "aws": {"eks": {"context": f"sova-test-k8s-{RUN}-eks"}},
        "gcp": {"gke": {"context": f"sova-test-k8s-{RUN}-gke"}}}}
    text = cloud_env.render_env(lease, paths, {})
    assert f"SOVA_TEST_KUBE_EKS_CONTEXT=sova-test-k8s-{RUN}-eks" in text
    assert f"SOVA_TEST_KUBE_GKE_CONTEXT=sova-test-k8s-{RUN}-gke" in text
    assert f"SOVA_TEST_KUBE_MANAGED_KUBECONFIG={paths.kubeconfig}" in text
