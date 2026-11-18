"""Actual image CLI, Pod identity, alive-owner refusal and stopped-Pod recovery.

Real Kubernetes, SQL and TLS; synthetic plan/identity-provider/bootstrap data.
An optional traffic fixture serves real HTTPS orders; the bare Pod journey
uses endpoint fixtures. No cloud or production
network/WORM-store qualification is claimed.
"""

from dataclasses import replace
from contextlib import ExitStack
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re

import pytest
from qualification_pods import (NAMESPACE, SERVICE_SOURCE, app_pod, command, owned_cluster,
                                prepare_workload, wait_for, worker_pod)
from qualification_service import observe_database_versions, service_journey
from qualification_diagnostics import capture_state
from test_order_http_handoff import http_business as http_business  # noqa: F401

from sova.adapters.file_authority_witness import FileAuthorityWitness
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.qualification.image import PROBE, compare, project_version, source_inventory

pytestmark = [pytest.mark.postgres, pytest.mark.kubernetes]


def sql_state(h):
    repo = PostgresLifecycleRepository(h.connect, tenant_id=h.tenant)
    with repo.transaction() as conn:
        row = conn.execute("SELECT owner_id,valid_until,clock_timestamp() FROM sova_control.app_execution "
                           "WHERE application_id=%s", (h.app,)).fetchone()
        recovery = conn.execute("SELECT owner_id,recorded_at FROM sova_control.execution_events "
                                "WHERE application_id=%s AND action='recovery_acquired'", (h.app,)).fetchall()
        effects = conn.execute("SELECT status FROM sova_control.effects WHERE application_id=%s "
                               "AND command_document->>'action'='commit_route'", (h.app,)).fetchall()
    return row, recovery, effects


def denied_pod_observation(env, observer_pod, lost, uid, denied_uid):
    observer = ("import json; from sova.service.quiescence import in_cluster_pod_state; "
                f"print(json.dumps({{'state':in_cluster_pod_state({NAMESPACE!r})({lost!r},{uid!r})}}))")
    assert json.loads(env.exec(observer_pod, observer)) == {"state": None}
    events = [e for e in env.audit_events() if e.get("stage") == "ResponseComplete"
              and e.get("responseStatus", {}).get("code") == 403
              and e.get("objectRef", {}).get("resource") == "pods"
              and e.get("objectRef", {}).get("name") == lost
              and e.get("user", {}).get("uid") == denied_uid]
    assert events, "Unknown Pod observation was not tied to an actual RBAC 403"
    env.report["unauthorized_pod_observation_status"] = 403
    env.report["unauthorized_pod_observation"] = "unknown"


def complete_ready_handoff(h, env, traffic, admitted, pods, service_accounts):
    """Normal cutover; owner-loss recovery is verified in the separate v2 cases."""
    assert traffic.ready_before_switch and traffic.prepared and traffic.activation_calls == 0
    status = wait_for(lambda: (s if (s := h.status(admitted["unit_digest"]))["state"]
                              == "HANDOFF_COMPLETE" else None), 90, "normal handoff completion by actual workers")
    _, recovered, effects = sql_state(h)
    assert recovered == [] and effects == [("confirmed_applied",)]
    patches = [e for e in env.audit_events() if e.get("stage") == "ResponseComplete"
               and e.get("verb") == "patch" and e.get("objectRef", {}).get("resource") == "services"
               and e["objectRef"].get("name") == "orders" and e.get("responseStatus", {}).get("code") == 200]
    assert len(patches) == 1 and patches[0]["user"]["uid"] == service_accounts["worker"]
    pod_uids = {p["metadata"]["uid"] for p in pods.values()}
    mutator = patches[0]["user"]["extra"]["authentication.kubernetes.io/pod-uid"]
    assert len(mutator) == 1 and mutator[0] in pod_uids
    assert any(status["history"][-1]["actor"].startswith(name+"."+p["metadata"]["uid"]+".")
               for name,p in pods.items())
    assert env.object("pod", "orders-target")["metadata"]["uid"] == traffic.target_uid
    assert env.object("pod", "orders-target")["metadata"]["labels"]["side"] == "target"
    env.report["http_business"] = traffic.finish()
    assert traffic.activation_calls == 0
    target_load = [e for e in traffic.events() if e["phase"] in ("load", "inline_retry")
                   and e["status"] == 200 and e["side"] == "target"]
    assert len(target_load) >= 3, "No target business responses during measured load"
    confirmed = env.report["http_business"]["reconciliation"]["counts"]["confirmed_requests"]
    with h.business["connect_as"](h.business["names"]["tgt"], h.business["app"]) as conn:
        assert conn.execute("SELECT count(*) FROM orders WHERE customer LIKE 'customer-%'").fetchone()[0] == 50
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 50 + confirmed
    env.report["handoff"] = {"state": status["state"], "owner_fault_injected": False,
        "recovery_events": 0, "route_patch_requests": 1, "route_mutator_pod_uid": mutator[0],
        "final_actor": status["history"][-1]["actor"], "effect_status": "confirmed_applied",
        "historical_orders_preserved": 50, "target_write_confirmed": True,
        "target_load_successes": len(target_load), "late_target_activation_calls": traffic.activation_calls,
        "client_continuity_qualified": False}
    env.phase("normal ready-target handoff completed by actual workers without injected owner loss")


def worker_pod_journey(postgres_database, http_business, tmp_path, *, traffic_factory=None):
    if not os.environ.get("SOVA_TEST_QUALIFICATION_RUN"):
        pytest.skip("Requires the dedicated --suite pods runner")
    artifacts = Path(os.environ["SOVA_TEST_QUALIFICATION_ARTIFACTS"])
    # Observe fixture metadata before the expensive cluster bootstrap. These
    # are actual SQL results, not supplied engine-version strings or retries.
    observed_versions = observe_database_versions(http_business)
    (artifacts / "database-bootstrap.json").write_text(json.dumps(observed_versions.document, indent=2) + "\n")
    with owned_cluster(artifacts / "pods") as env, ExitStack() as resources:
        env.report["database_bootstrap"] = observed_versions.document
        with service_journey(postgres_database, http_business, env.witness, observed_versions=observed_versions) as h:
            # Preserve status-only host observations even if a fail-closed read
            # aborts the journey. No tokens or response bodies are recorded.
            env.report["control_plane_http_calls"] = h.trace
            resources.callback(capture_state, h, env)
            # Both actual CLI workers and the host API use this fixture witness.
            h.settings = replace(h.settings, witness_for=lambda tenant: FileAuthorityWitness(
                env.witness / (tenant + ".log"), tenant_id=tenant))
            h.plane.settings = h.settings
            wait_for(lambda: env.kube("-n", NAMESPACE, "get", "configmap", "kube-root-ca.crt",
                                      "--ignore-not-found", "-o", "json"), 30, "namespace CA")
            sides = prepare_workload(env, h, *postgres_database)
            traffic = resources.enter_context(traffic_factory(env, h)) if traffic_factory else None
            env.apply(traffic.pod("orders-source", "source") if traffic else app_pod(env, "orders-source", "source"))
            env.kube("-n", NAMESPACE, "wait", "--for=condition=Ready", "pod/orders-source", "--timeout=60s")
            compiled = h.register_forward()
            h.approve(compiled, "pod-worker-approval")
            # Finish identity/authorization observations before admitting work.
            # Precreate the synthetic file so an already running worker never
            # races the host's first append and permission adjustment.
            witness_path = env.witness / (h.tenant + ".log")
            witness_path.touch()
            witness_path.chmod(0o666)
            env.apply(worker_pod(env, "worker-a"), worker_pod(env, "worker-b"))
            for name in ("worker-a", "worker-b"):
                env.kube("-n", NAMESPACE, "wait", "--for=condition=Ready", "pod/" + name, "--timeout=90s")
            env.phase("both actual CLI worker Pods ready")
            pods = {name: env.object("pod", name) for name in ("worker-a", "worker-b")}
            service_accounts = {name: env.object("serviceaccount", name)["metadata"]["uid"]
                                for name in ("worker", "denied")}
            identities = []
            cri = env.report["loaded_cri_image"]
            image_digests = set(re.findall(r"sha256:[0-9a-f]{64}", json.dumps(cri)))
            for name, pod in pods.items():
                status = pod["status"]["containerStatuses"][0]
                assert re.findall(r"sha256:[0-9a-f]{64}", status["imageID"])[-1] in image_digests
                identities.append({"name": name, "uid": pod["metadata"]["uid"],
                                   "service_account": pod["spec"]["serviceAccountName"],
                                   "service_account_uid": service_accounts["worker"],
                                   "image_id": status["imageID"], "container_id": status["containerID"]})
            # Read installed bytes from a real Pod, not from its image label.
            root = Path(__file__).resolve().parents[2]
            security = Path(os.environ["SOVA_TEST_QUALIFICATION_SECURITY_SOURCE"])
            source = SERVICE_SOURCE
            producer = "27e6e85514a7cbb3e43d216464b72d9bd78f3fcf"
            expected = {"sova": source_inventory(root, source, "sova"),
                        "sova_security": source_inventory(security, producer, "sova_security")}
            versions = {"sova": project_version(root, source), "sova_security": project_version(security, producer)}
            payload = compare(expected, json.loads(env.exec("worker-b", PROBE)), versions)
            assert payload["status"] == "MATCH"
            env.report["pod_identities"] = identities
            env.report["installed_pod_source_match"] = payload
            env.report["secret_read_allowed"] = env.kube("auth", "can-i", "get", "secrets", "-n", NAMESPACE,
                "--as", f"system:serviceaccount:{NAMESPACE}:worker", check=False).strip()
            assert env.report["secret_read_allowed"] == "no"
            secret_probe = ("import json,ssl; from pathlib import Path; import httpx; "
                "p=Path('/var/run/secrets/kubernetes.io/serviceaccount'); "
                f"r=httpx.get('https://kubernetes.default.svc/api/v1/namespaces/{NAMESPACE}/secrets/worker-config', "
                "headers={'Authorization':'Bearer '+(p/'token').read_text().strip()}, "
                "verify=ssl.create_default_context(cafile=str(p/'ca.crt')),timeout=5); "
                "print(json.dumps({'status':r.status_code}))")
            assert json.loads(env.exec("worker-b", secret_probe)) == {"status": 403}
            env.report["actual_worker_secret_read_status"] = 403
            if traffic and traffic.ready_before_switch:
                denied_pod_observation(env, traffic.observer_pod, "worker-a",
                                       pods["worker-a"]["metadata"]["uid"], service_accounts["denied"])
            if traffic:
                traffic.start()
                env.phase("continuous HTTPS orders started at fixed Service origin")
            admitted = h.admit("pod-worker-approval")
            env.report["owner_fault_injected"] = not (traffic and traffic.ready_before_switch)
            env.phase("work admitted after identity and authorization observations")
            if traffic and traffic.ready_before_switch:
                complete_ready_handoff(h, env, traffic, admitted, pods, service_accounts)
                return

            def route_started():
                route = env.object("service", "orders")["spec"]["selector"]
                row, _, effects = sql_state(h)
                return (row if route == sides[h.business["names"]["tgt"]]
                        and effects == [("in_flight",)] else None)
            row = wait_for(route_started, 90, "actual route mutation with effect still in flight")
            owner = row[0]
            lost, uid, instance = owner.split(".")
            assert lost in pods and uid == pods[lost]["metadata"]["uid"]
            assert re.fullmatch(r"[0-9a-f]{12}", instance)
            successor = "worker-b" if lost == "worker-a" else "worker-a"
            container = pods[lost]["status"]["containerStatuses"][0]["containerID"].split("://")[1]

            # A runtime pause freezes the keeper too, but leaves a real Running
            # Pod. It is not proof of death and must not permit recovery.
            command(["docker", "exec", env.node, "ctr", "--namespace", "k8s.io", "tasks", "pause", container])
            paused_at = datetime.now(UTC)
            (env.directory / "owner-before-pause.log").write_text(env.kube("-n", NAMESPACE, "logs", lost))
            env.phase("owner task paused; Pod is still Running")
            # The target endpoint must be ready before recovery's readback;
            # the old owner remains frozen and cannot finish its own step.
            if traffic:
                traffic.activate_target()
            else:
                env.apply(app_pod(env, "orders-target", "target"))
            env.kube("-n", NAMESPACE, "wait", "--for=condition=Ready", "pod/orders-target", "--timeout=60s")
            wait_for(lambda: sql_state(h)[0][1] < sql_state(h)[0][2], 20, "real database lease expiry")
            row, recovered, effects = sql_state(h)
            assert row[0] == owner and row[1] < row[2]
            expired_observed_at = row[2]
            assert recovered == [] and effects == [("in_flight",)]
            assert env.object("pod", lost)["status"]["phase"] == "Running"
            def observed_alive():
                return [e for e in env.audit_events() if e.get("stage") == "ResponseComplete"
                        and e.get("verb") == "get" and e.get("objectRef", {}).get("name") == lost
                        and e.get("responseStatus", {}).get("code") == 200
                        and datetime.fromisoformat(e["requestReceivedTimestamp"].replace("Z", "+00:00")) >= paused_at]
            # Audit visibility and a successor's real HTTPS query can lag its
            # polling interval. Wait for evidence, not an arbitrary sleep.
            alive_checks = wait_for(observed_alive, 15, "authenticated live-Pod observation after lease expiry")
            row, recovered, effects = sql_state(h)
            assert row[0] == owner and row[1] < row[2]
            assert recovered == [] and effects == [("in_flight",)]
            assert env.object("pod", lost)["status"]["phase"] == "Running"
            assert all(e["user"]["uid"] == service_accounts["worker"]
                       and e["user"]["extra"]["authentication.kubernetes.io/pod-uid"]
                           == [pods[successor]["metadata"]["uid"]] for e in alive_checks)
            env.report["expired_lease_live_pod"] = {"owner_id": owner, "lease_until": row[1].isoformat(),
                "database_observed_at": row[2].isoformat(), "recovery_events": 0,
                "effect": "in_flight", "authenticated_alive_observations": len(alive_checks)}

            # A different real SA cannot read Pods. 403 must become unknown,
            # never a fabricated False/dead-Pod observation.
            denied_pod_observation(env, traffic.observer_pod if traffic else "orders-source",
                                   lost, uid, service_accounts["denied"])

            env.phase("lease expired but recovery refused for live or unauthorized Pod observation")
            # Normal kubelet deletion, not --force/zero grace (which by itself
            # could remove the API object before its process really stopped).
            watched = env.object("pod", lost)
            with env.watch_pod(lost, uid, watched["metadata"]["resourceVersion"]) as watch_path:
                deletion_requested_at = datetime.now(UTC)
                env.kube("-n", NAMESPACE, "delete", "pod", lost, "--grace-period=1", "--wait=true")
            terminal = [event for line in watch_path.read_text().splitlines()
                        if (event := json.loads(line))["phase"] in ("Succeeded", "Failed")]
            stopped_at = datetime.now(UTC)
            all_containers = json.loads(command(["docker", "exec", env.node, "crictl", "ps", "-a", "-o", "json"]).stdout)
            old = [c for c in all_containers["containers"] if c["id"] == container]
            assert not old or old[0]["state"] == "CONTAINER_EXITED"
            env.report["runtime_stop"] = {"container_id": container, "observed_at": stopped_at.isoformat(),
                "state": "CONTAINER_EXITED" if old else "absent from CRI inventory", "force_api_deletion": False,
                "terminal_pod_watch": terminal}
            env.phase("old Pod gone and runtime container confirmed stopped")
            status = wait_for(lambda: (s if (s := h.status(admitted["unit_digest"]))["state"]
                                      == "HANDOFF_COMPLETE" else None), 90, "handoff completion by actual successor")
            row, recovered, effects = sql_state(h)
            assert len(recovered) == 1 and recovered[0][0].startswith(successor + ".")
            assert recovered[0][0].split(".")[1] == pods[successor]["metadata"]["uid"]
            assert effects == [("confirmed_applied",)]
            assert status["history"][-1]["actor"].startswith(successor + ".")
            assert recovered[0][1] >= expired_observed_at
            # Metadata-only API audit proves that the route was patched once,
            # including across the crash; a single effect row alone cannot.
            patches = [e for e in env.audit_events() if e.get("stage") == "ResponseComplete"
                       and e.get("verb") == "patch" and e.get("objectRef", {}).get("resource") == "services"
                       and e["objectRef"].get("name") == "orders" and e.get("responseStatus", {}).get("code") == 200]
            assert len(patches) == 1, len(patches)
            assert patches[0]["user"]["uid"] == service_accounts["worker"]
            assert patches[0]["user"]["extra"]["authentication.kubernetes.io/pod-uid"] == [uid]
            gone = [e for e in env.audit_events() if e.get("stage") == "ResponseComplete" and e.get("verb") == "get"
                    and e.get("objectRef", {}).get("name") == lost and e.get("responseStatus", {}).get("code") == 404]
            terminal_gets = [e for e in env.audit_events() if terminal and e.get("stage") == "ResponseComplete"
                            and e.get("verb") == "get" and e.get("objectRef", {}).get("name") == lost
                            and e.get("responseStatus", {}).get("code") == 200
                            and datetime.fromisoformat(e["requestReceivedTimestamp"].replace("Z", "+00:00")) >= deletion_requested_at]
            stopped_observations = gone + terminal_gets
            assert stopped_observations, "No authenticated missing/terminal Pod observation with runtime stop proof"
            assert all(e["user"]["uid"] == service_accounts["worker"]
                       and e["user"]["extra"]["authentication.kubernetes.io/pod-uid"]
                           == [pods[successor]["metadata"]["uid"]] for e in stopped_observations)
            if traffic:
                env.report["http_business"] = traffic.finish()
                confirmed = env.report["http_business"]["reconciliation"]["counts"]["confirmed_requests"]
            with h.business["connect_as"](h.business["names"]["tgt"], h.business["app"]) as conn:
                assert conn.execute("SELECT count(*) FROM orders WHERE customer LIKE 'customer-%'").fetchone()[0] == 50
                if traffic:
                    assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 50 + confirmed
                else:
                    conn.execute("INSERT INTO orders VALUES (9001,'pod-after','pod-after',1)")
            env.report["handoff"] = {"state": status["state"], "original_owner": owner,
                "recovery_owner": recovered[0][0], "recovery_at": recovered[0][1].isoformat(),
                "route_patch_requests": len(patches), "pod_absence_audit_observations": len(gone),
                "stopped_pod_observation_http_statuses": sorted({e["responseStatus"]["code"] for e in stopped_observations}),
                "effect_status": effects[0][0], "historical_orders_preserved": 50, "target_write_confirmed": True,
                "client_continuity_qualified": False}
            env.phase("actual successor completed handoff without reissuing route mutation")


def test_fixed_worker_image_reconciles_only_after_lease_expiry_and_real_pod_stop(
        postgres_database, http_business, tmp_path):
    worker_pod_journey(postgres_database, http_business, tmp_path)
