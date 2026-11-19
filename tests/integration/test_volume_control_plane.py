"""A file-state application's volume handed off by the whole control plane:
registered, compiled, approved and admitted through the service, then run by
a worker - the volume adapter through the effect ledger, every obligation
measured by the volume probes and signed, the authority advanced only by the
coordinator. The application is the SQLite-and-uploads board of
``volume_business``; another tenant's volume lives in a namespace of its own.
A contract may also require that no other workload reaches the unit's
Secrets on the target (SEC-01.1): the worker measures it with tokens it
issues for those workloads and the API server's own decision."""

import json
import threading

from kind_support import storage_class
from control_plane_support import board_contract, prepared, signed_results, worker_for
import pytest
from volume_business import PYTHON, UID
from volume_world import Route, VolumeWorld

from sova.domain.handoff_authority import HandoffState
from sova.service.volume_execution import volume_factory

pytestmark = [pytest.mark.postgres, pytest.mark.kubernetes]
FILES = "SELECT attachment FROM posts WHERE attachment IS NOT NULL"


def board_settings(board, controls, other):
    """What the execution file says about the board's volume unit."""
    route = Route(controls, "board", {"board_src": {"app": "board", "side": "source"},
                                      "board_tgt": {"app": "board", "side": "target"}},
                  converge_seconds=60)
    return {"source": board._side("source", controls), "target": board._side("target", controls),
            "route": route, "routes": ("board_src", "board_tgt"), "agent_key": VolumeWorld.KEY,
            "agent_image": PYTHON, "run_as": (UID, UID), "fs_group": UID,
            "storage_class": storage_class(),
            "write_probe": (8080, "/probe"), "databases": ("board.db",),
            "invariants": {"BOARD-01": {"database": "board.db", "under": "uploads",
                                        "files": FILES}},
            "zone_of": lambda side: "approved-zone-a",  # the operator's inventory
            "other": (other, "records"), "scratch_claim": "sova-restore"}


def _handoff(postgres_database, now, volume_business, other_tenant, tmp_path, strategy):
    h = prepared(postgres_database, now, tmp_path, board_contract(), engine="kubernetes-volume",
                 strategy=strategy)
    board = VolumeWorld(volume_business)
    settings = board_settings(board, volume_business["controls"](), other_tenant)
    h["worker"] = worker_for(h, volume_factory(h["adapter_id"], lambda t, a: settings),
                             owner="worker-volume")
    return {**h, "board": board, "route": settings["route"]}


@pytest.fixture
def volume_handoff(postgres_database, now, volume_business, other_tenant, tmp_path):
    return _handoff(postgres_database, now, volume_business, other_tenant, tmp_path,
                    "offline-copy")


@pytest.fixture
def volume_cdc_handoff(postgres_database, now, volume_business, other_tenant, tmp_path):
    return _handoff(postgres_database, now, volume_business, other_tenant, tmp_path,
                    "snapshot-cdc")


def test_a_volume_unit_is_handed_off_by_the_control_plane(volume_handoff):
    h, board = volume_handoff, volume_handoff["board"]
    assert h["adapter_id"] == "volume-offline-copy"
    board.business_write(2)
    before = board.posts("source")
    record = h["worker"].drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert h["route"].get() == "board_tgt"
    assert board.posts("target") == before  # rows, integrity and every upload
    assert not board.can_write("source", "app") and not board.can_write("source", "other")
    assert board.can_write("target", "app") and not board.can_write("target", "other")
    # Every obligation was measured by a probe and signed, none failed.
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    measured = {o.obligation_id for o in h["plan"].obligations}
    assert measured <= set(results), sorted(measured - set(results))
    # Nothing of the probes or the adapter is left behind.
    pods = board._pods()
    assert not [p["metadata"]["name"] for p in pods
                if p["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") == "sova"]
    claims = json.loads(board.run("get", "pvc", "-o", "json"))["items"]
    assert not [c for c in claims if c["metadata"]["name"].startswith("sova-restore")
                and not c["metadata"].get("deletionTimestamp")]
    seen = h["observations"]
    assert seen["data.board.db"]["method"] == "sqlite_logical"
    assert seen["business.BOARD-01"]["missing"] == [] and seen["business.BOARD-01"]["referenced"]
    assert seen["fence.source_writes_denied"]["unchanged"] is True
    print("measured:", json.dumps({k: seen[k] for k in sorted(seen)
                                   if k.startswith(("availability.", "operations."))}))


def test_a_volume_unit_is_copied_before_the_stop_on_the_snapshot_cdc_template(
        volume_cdc_handoff):
    """The bulk reaches the target while the board serves (``start_sync``); the
    stop carries only what changed since, and the restore drill, which needs
    the data on the target, is measured before the stop (ADR-0003)."""
    h, board = volume_cdc_handoff, volume_cdc_handoff["board"]
    assert h["adapter_id"] == "volume-snapshot-cdc"
    board.business_write(2)
    before = board.posts("source")
    record = h["worker"].drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert h["route"].get() == "board_tgt"
    assert board.posts("target") == before
    assert not board.can_write("source", "app") and not board.can_write("source", "other")
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    measured = {o.obligation_id for o in h["plan"].obligations}
    assert measured <= set(results), sorted(measured - set(results))
    seen = h["observations"]
    order = list(seen)  # in the order they were first measured
    assert order.index("security.tested_restore") < order.index("fence.source_writes_denied")
    assert seen["fence.source_writes_denied"]["unchanged"] is True
    print("measured:", json.dumps({k: seen[k] for k in sorted(seen)
                                   if k.startswith(("availability.", "operations."))}))


def test_a_board_written_throughout_the_sync_is_handed_off(volume_cdc_handoff):
    """Posts keep coming while the files are copied before the stop: the
    target's database then is a copy of a file in use (SQLite checkpoints and
    removes its log as each request's connection closes). The plan neither
    fails on it before the stop nor loses a post acknowledged before it."""
    h, board = volume_cdc_handoff, volume_cdc_handoff["board"]
    done, acknowledged = threading.Event(), []

    def keep_posting():
        while not done.is_set():
            try:
                board.business_write(1)
            except Exception:  # the board stopped serving: the write stop began
                if acknowledged:
                    return
                continue
            acknowledged.append(1)

    writer = threading.Thread(target=keep_posting, daemon=True)
    writer.start()
    try:
        record = h["worker"].drive(h["tenant"], h["app"], h["unit"])
    finally:
        done.set()
        writer.join(60)
    drill = h["observations"].get("security.tested_restore")
    print("restore drill:", json.dumps(drill)[:1500])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert len(acknowledged) >= 3, len(acknowledged)  # the board was written during the sync
    target = board.posts("target")
    assert target["integrity"] == "ok"
    assert len(target["posts"]) >= 20 + len(acknowledged), (len(target["posts"]),
                                                            len(acknowledged))
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}


def secret_access(request, volume_business, *, granted):
    """The board's database password as a Secret beside it, another workload's
    service account (``billing``), and what the worker is allowed for the
    check: get that Secret, issue that account's tokens, review access.
    ``granted`` gives ``billing`` a read of the Secret."""
    import base64

    from kind_support import kubectl

    from sova.adapters.kubernetes_secret_probes import SecretAccessControls, ServiceAccount
    from sova.service.secret_access import probes_from

    context, namespace = volume_business["context"], volume_business["namespace"]

    def role(name, rules, subject):
        return [{"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                 "metadata": {"name": name}, "rules": rules},
                {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
                 "metadata": {"name": name},
                 "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                             "name": name},
                 "subjects": [{"kind": "ServiceAccount", "name": subject}]}]

    items = [{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "board-db"},
              "data": {"password": base64.b64encode(b"board-secret-test-value").decode()}},
             {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "billing"}}]
    items += role("sova-secret-check", [
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["board-db"],
         "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["serviceaccounts/token"],
         "resourceNames": ["billing"], "verbs": ["create"]}], "sova-handoff")
    if granted:
        items += role("billing-reads-board-db", [
            {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["board-db"],
             "verbs": ["get"]}], "billing")
    volume_business["run"]("apply", "-f", "-", stdin=json.dumps(
        {"apiVersion": "v1", "kind": "List", "items": items}))
    reviewer = f"sova-access-review-{namespace}"
    kubectl(context, "apply", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "List", "items": [
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
             "metadata": {"name": reviewer},
             "rules": [{"apiGroups": ["authorization.k8s.io"],
                        "resources": ["subjectaccessreviews"], "verbs": ["create"]}]},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
             "metadata": {"name": reviewer},
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                         "name": reviewer},
             "subjects": [{"kind": "ServiceAccount", "name": "sova-handoff",
                           "namespace": namespace}]}]}))
    request.addfinalizer(lambda: kubectl(context, "delete", "clusterrolebinding,clusterrole",
                                         reviewer, "--wait=false"))
    ca = base64.b64decode(kubectl(
        context, "config", "view", "--raw", "--minify", "-o",
        "jsonpath={.clusters[0].cluster.certificate-authority-data}").strip())
    token = volume_business["run"]("create", "token", "sova-handoff",
                                   "--duration=3600s").strip()
    platform = SecretAccessControls(volume_business["controls"]().registration, ca_pem=ca,
                                    token_provider=lambda: token)
    return probes_from(platform, secrets=["board-db"],
                       others=[ServiceAccount(namespace, "billing")])


def _secret_handoff(request, postgres_database, now, volume_business, other_tenant, tmp_path,
                    *, granted):
    from sova.service.secret_access import with_secret_access

    data = board_contract()
    data["spec"]["security"]["required_controls"].append("deny_cross_workload_secret_access")
    h = prepared(postgres_database, now, tmp_path, data, engine="kubernetes-volume")
    board = VolumeWorld(volume_business)
    settings = board_settings(board, volume_business["controls"](), other_tenant)
    probes = secret_access(request, volume_business, granted=granted)
    h["worker"] = worker_for(h, with_secret_access(
        volume_factory(h["adapter_id"], lambda t, a: settings), probes), owner="worker-volume")
    return {**h, "board": board, "route": settings["route"]}


def test_the_target_keeps_the_units_secrets_from_other_workloads(
        request, postgres_database, now, volume_business, other_tenant, tmp_path):
    h = _secret_handoff(request, postgres_database, now, volume_business, other_tenant,
                        tmp_path, granted=False)
    record = h["worker"].drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert signed_results(h)["security.deny_cross_workload_secret_access"] == "pass"
    seen = h["observations"]["security.deny_cross_workload_secret_access"]
    assert seen["outcome"] == "refused" and "board-secret-test-value" not in json.dumps(seen)


def test_another_workload_able_to_read_the_units_secret_holds_the_handoff(
        request, postgres_database, now, volume_business, other_tenant, tmp_path):
    h = _secret_handoff(request, postgres_database, now, volume_business, other_tenant,
                        tmp_path, granted=True)
    board = h["board"]
    record = h["worker"].drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.SAFE_HOLD and record.reason == "CONTRACT_BREACH"
    assert not record.activation_possible
    assert signed_results(h)["security.deny_cross_workload_secret_access"] == "fail"
    assert h["route"].get() == "board_src" and board.can_write("source", "app")
