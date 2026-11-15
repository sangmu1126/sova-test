"""GCP adapters on recorded REST answers: the rules that do not need a cloud."""

import base64
import json

import pytest

from sova.adapters.gcp_rest import GcpApiError, GcpRest
from sova.adapters.gcp_secret_manager import GcpSecretManagerSource, crc32c
from sova.domain.contracts import ContractError


class Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self.content = b"" if body is None else json.dumps(body).encode()
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class Session:
    """Answers by (method, URL prefix); records every request."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def request(self, method, url, json=None, params=None, timeout=None):
        self.requests.append((method, url, json, params))
        for (m, prefix), answer in self.routes.items():
            if m == method and url.startswith(prefix):
                return answer(url, json, params) if callable(answer) else answer
        return Response(404, {"error": {"status": "NOT_FOUND", "message": "x"}})


def rest(routes):
    return GcpRest(Session(routes), "sova-test-project")


def test_errors_carry_status_and_reason_never_the_body():
    r = rest({("GET", "https://x/"): Response(403, {"error": {
        "status": "PERMISSION_DENIED", "message": "secret value hunter2 quoted"}})})
    with pytest.raises(GcpApiError) as caught:
        r.get("https://x/a?token=abc")
    assert caught.value.status == 403 and caught.value.reason == "PERMISSION_DENIED"
    assert "hunter2" not in str(caught.value) and "token=abc" not in str(caught.value)


def test_missing_is_none_only_when_asked():
    r = rest({})
    assert r.get("https://x/y", missing_ok=True) is None
    with pytest.raises(GcpApiError):
        r.get("https://x/y")


def test_pages_follow_the_token_and_are_bounded():
    def page(url, body, params):
        token = (params or {}).get("pageToken")
        return Response(200, {"items": [token or "first"],
                              **({} if token == "2" else {"nextPageToken": str(int(token or 0) + 1)})})

    r = rest({("GET", "https://x/list"): page})
    assert r.pages("https://x/list", "items") == ["first", "1", "2"]
    with pytest.raises(ContractError):
        r.pages("https://x/list", "items", limit=1)


def test_a_project_id_is_required():
    with pytest.raises(ContractError):
        GcpRest(Session({}), "Not A Project")


SECRET = "https://secretmanager.googleapis.com/v1/projects/sova-test-project/secrets/"


def secret_answer(value, version="7", checksum=True):
    payload = {"data": base64.b64encode(value).decode()}
    if checksum is True:
        payload["dataCrc32c"] = str(crc32c(value))
    elif checksum is not None:
        payload["dataCrc32c"] = str(checksum)
    return Response(200, {"name": f"projects/123/secrets/db/versions/{version}",
                          "payload": payload})


def test_the_latest_version_is_read_and_checked():
    source = GcpSecretManagerSource(rest({("GET", SECRET + "db/versions/latest:access"):
                                          secret_answer(b"pw-1")}))
    assert source.current("db") == ("7", b"pw-1")


def test_a_corrupted_read_never_reaches_the_target():
    source = GcpSecretManagerSource(rest({("GET", SECRET + "db/"): secret_answer(
        b"pw-1", checksum=crc32c(b"pw-2"))}))
    with pytest.raises(ContractError, match="checksum") as caught:
        source.current("db")
    assert "pw-1" not in str(caught.value)


def test_an_unreadable_source_says_so_without_the_providers_message():
    source = GcpSecretManagerSource(rest({("GET", SECRET): Response(403, {"error": {
        "status": "PERMISSION_DENIED", "message": "caller lacks access to db"}})}))
    with pytest.raises(ContractError, match="Source secret unavailable: db") as caught:
        source.current("db")
    assert "caller lacks" not in str(caught.value)


@pytest.mark.parametrize("secret_id", ["", "a/b", "x" * 256, "db?x=1"])
def test_only_secret_ids_are_accepted(secret_id):
    with pytest.raises(ContractError):
        GcpSecretManagerSource(rest({})).current(secret_id)


# -- DNS: the query, and the Cloud DNS route store ---------------------------------------

import struct  # noqa: E402

from sova.adapters.dns_query import _encode, parse_reply  # noqa: E402
from sova.adapters.gcp_cloud_dns_route import CloudDnsRoute  # noqa: E402


def reply(query_id, name, addresses, *, flags=0x8400, rcode=0, owner=None):
    question = _encode(name) + struct.pack("!HH", 1, 1)
    answers = b""
    for address in addresses:
        answers += (b"\xc0\x0c" if owner is None else _encode(owner)) + struct.pack(
            "!HHIH", 1, 1, 60, 4) + bytes(int(p) for p in address.split("."))
    return struct.pack("!6H", query_id, flags | rcode, 1, len(addresses), 0, 0) + question + answers


def test_a_reply_gives_the_owner_names_a_records():
    assert parse_reply(reply(7, "app.example.test.", ["192.0.2.2", "192.0.2.1"]), 7,
                       "app.example.test.") == ("NOERROR", True, ["192.0.2.1", "192.0.2.2"])


def test_replies_to_other_queries_or_names_are_rejected():
    with pytest.raises(ValueError):
        parse_reply(reply(8, "app.example.test.", ["192.0.2.1"]), 7, "app.example.test.")
    with pytest.raises(ValueError):
        parse_reply(reply(7, "other.example.test.", ["192.0.2.1"]), 7, "app.example.test.")
    # A record for another owner in the answer section is not this name's address.
    assert parse_reply(reply(7, "app.example.test.", ["192.0.2.9"], owner="x.example.test."),
                       7, "app.example.test.")[2] == []


def test_a_truncated_or_refused_reply_has_no_addresses():
    assert parse_reply(reply(7, "a.example.test.", ["192.0.2.1"], flags=0x8600), 7,
                       "a.example.test.") == ("TRUNCATED", True, [])
    assert parse_reply(reply(7, "a.example.test.", [], rcode=5), 7,
                       "a.example.test.")[0] == "REFUSED"


ZONE = "https://dns.googleapis.com/dns/v1/projects/sova-test-project/managedZones/z1"
SIDES = {"src": "192.0.2.10", "tgt": "192.0.2.20"}


class DnsWorld:
    def __init__(self, record="192.0.2.10", answers=None):
        self.record = record
        self.answers = answers or {}
        self.changes = []

    def routes(self):
        def rrset(url, body, params):
            if self.record is None:
                return Response(404, {"error": {"status": "NOT_FOUND"}})
            return Response(200, {"name": "app.example.test.", "type": "A", "ttl": 60,
                                  "rrdatas": [self.record]})

        def change(url, body, params):
            deleted = (body.get("deletions") or [{}])[0].get("rrdatas")
            if self.record is not None and deleted != [self.record]:
                return Response(412, {"error": {"status": "FAILED_PRECONDITION"}})
            self.changes.append(body)
            self.record = body["additions"][0]["rrdatas"][0]
            return Response(200, {"id": str(len(self.changes)), "status": "pending"})

        return {("GET", ZONE + "/rrsets/"): rrset, ("POST", ZONE + "/changes"): change,
                ("GET", ZONE + "/changes/"): Response(200, {"status": "done"}),
                ("GET", ZONE): Response(200, {"dnsName": "example.test.", "nameServers": [
                    "ns-1.example.net.", "ns-2.example.net."]})}

    def resolver(self, server, name):
        return self.answers.get(server, ("NOERROR", True, [self.record]))


def dns_route(world):
    return CloudDnsRoute(rest(world.routes()), zone="z1", name="app.example.test", sides=SIDES,
                         resolver=world.resolver, sync_wait_seconds=1)


def test_a_side_only_when_record_and_every_server_agree():
    world = DnsWorld()
    route = dns_route(world)
    assert route.get() == "src"
    world.answers = {"ns-2.example.net.": ("NOERROR", True, ["192.0.2.99"])}
    assert route.get() is None  # one server still answers something else
    world.answers = {"ns-1.example.net.": ("SERVFAIL", False, [])}
    assert route.get() is None


def test_set_swaps_atomically_and_refuses_a_record_changed_in_between():
    world = DnsWorld()
    route = dns_route(world)
    route.set("tgt")
    assert world.changes[-1]["deletions"][0]["rrdatas"] == ["192.0.2.10"]
    assert route.get() == "tgt"
    route.set("tgt")  # already there: no change
    assert len(world.changes) == 1

    class Racing(DnsWorld):
        def routes(self):
            routes = super().routes()
            read = routes[("GET", ZONE + "/rrsets/")]

            def stale(url, body, params):
                answer = read(url, body, params)
                self.record = "192.0.2.77"  # someone else edits right after our read
                return answer

            routes[("GET", ZONE + "/rrsets/")] = stale
            return routes

    with pytest.raises(ContractError, match="changed in between"):
        dns_route(Racing()).set("tgt")


# -- Identity Platform directory ------------------------------------------------------------

from sova.adapters.gcp_identity_platform_directory import IdentityPlatformDirectory  # noqa: E402
from sova.adapters.keycloak_identity import AccountBoundExceeded  # noqa: E402

ACCOUNTS = "https://identitytoolkit.googleapis.com/v1/projects/sova-test-project"


def users_page(users, token=None):
    return Response(200, {"users": users, **({"nextPageToken": token} if token else {})})


def test_accounts_drop_hashes_and_keep_uid_groups_and_federation():
    users = [
        {"localId": "uid-a", "email": "Alice@Example.test", "emailVerified": True,
         "displayName": "Alice Tester", "passwordHash": "c2VjcmV0", "salt": "c2FsdA==",
         "customAttributes": json.dumps({"groups": ["finance"]}),
         "providerUserInfo": [{"providerId": "password"}]},
        {"localId": "uid-g", "email": "gina@example.test", "disabled": True,
         "providerUserInfo": [{"providerId": "google.com"}]},
    ]
    session = Session({("GET", ACCOUNTS + "/tenants/sova-test-x1-abcde/accounts:batchGet"):
                       users_page(users)})
    found = IdentityPlatformDirectory(GcpRest(session, "sova-test-project"),
                                      tenant="sova-test-x1-abcde").accounts(10)
    assert found[0] == {"subject": "uid-a", "username": "alice@example.test",
                        "email": "alice@example.test", "firstName": "Alice",
                        "lastName": "Tester", "enabled": True, "emailVerified": True,
                        "groups": ["/finance"], "federated": False}
    assert found[1]["federated"] is True and found[1]["enabled"] is False
    assert "c2VjcmV0" not in json.dumps(found) and users[0]["passwordHash"] is None


def test_accounts_follow_pages_and_respect_the_bound():
    def page(url, body, params):
        if params.get("nextPageToken") == "2":
            return users_page([{"localId": "u2", "email": "b@example.test"}])
        return users_page([{"localId": "u1", "email": "a@example.test"}], token="2")

    directory = IdentityPlatformDirectory(GcpRest(Session(
        {("GET", ACCOUNTS + "/accounts:batchGet"): page}), "sova-test-project"))
    assert [a["subject"] for a in directory.accounts(10)] == ["u1", "u2"]
    with pytest.raises(AccountBoundExceeded):
        directory.accounts(1)
    assert directory.issuer == "https://securetoken.google.com/sova-test-project"


def test_a_malformed_groups_claim_is_an_error_not_no_groups():
    directory = IdentityPlatformDirectory(GcpRest(Session({("GET", ACCOUNTS): users_page([
        {"localId": "u1", "email": "a@example.test",
         "customAttributes": json.dumps({"groups": "finance"})}])}), "sova-test-project"))
    with pytest.raises(ContractError, match="groups"):
        directory.accounts(10)


# -- Cloud Run compute -----------------------------------------------------------------------

from datetime import UTC, datetime, timedelta  # noqa: E402

from sova.adapters.gcp_cloud_run_compute import CloudRunServiceCompute  # noqa: E402
from sova.domain.contracts import EffectCommand, EffectStatus, ResourceScope  # noqa: E402

RUN_URL = ("https://run.googleapis.com/v2/projects/sova-test-project/locations/"
           "asia-northeast3/services/orders")


def compute_command(adapter):
    return EffectCommand(
        ResourceScope("tenant-a", "orders", "gcp-source", "unit-a"), "operation-a",
        "stop", adapter.adapter_id, "stop_source_compute", "operation-a:stop", "d" * 64,
        "revision-1", 1, datetime.now(UTC) + timedelta(minutes=5), json.dumps({}))


class RunService:
    def __init__(self):
        self.state = {"name": "orders", "generation": "3", "observedGeneration": "3",
                      "scaling": {"minInstanceCount": 1}, "etag": "e3",
                      "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
                      "uri": "https://orders-x.a.run.app", "urls": ["https://orders-x.a.run.app"]}
        self.patches = []

    def routes(self):
        def patch(url, body, params):
            if body.get("etag") != self.state["etag"]:
                return Response(409, {"error": {"status": "ABORTED"}})
            self.patches.append((body, params))
            generation = str(int(self.state["generation"]) + 1)
            self.state.update(scaling=body["scaling"], generation=generation,
                              observedGeneration=generation, etag="e" + generation)
            return Response(200, {"name": "operations/1"})

        return {("GET", RUN_URL): lambda u, b, p: Response(200, dict(self.state)),
                ("PATCH", RUN_URL): patch}


def test_stopping_means_manual_scaling_to_zero_read_back_from_cloud_run():
    service = RunService()
    adapter = CloudRunServiceCompute("run-src", rest=rest(service.routes()),
                                     region="asia-northeast3", service="orders",
                                     stop_wait_seconds=1)
    assert adapter.readback(compute_command(adapter), datetime.now(UTC)).status == \
        EffectStatus.UNKNOWN
    adapter.apply(compute_command(adapter))
    body, params = service.patches[0]
    assert body["scaling"] == {"scalingMode": "MANUAL", "manualInstanceCount": 0}
    assert body["etag"] == "e3" and params == {"updateMask": "scaling"}
    assert adapter.readback(compute_command(adapter), datetime.now(UTC)).status == \
        EffectStatus.APPLIED
    adapter.apply(compute_command(adapter))  # idempotent: no second change
    assert len(service.patches) == 1
    assert adapter.endpoints() == ["https://orders-x.a.run.app"]


def test_a_change_not_yet_reconciled_or_failed_is_not_stopped():
    service = RunService()
    service.state.update(scaling={"scalingMode": "MANUAL", "manualInstanceCount": 0})
    adapter = CloudRunServiceCompute("run-src", rest=rest(service.routes()),
                                     region="asia-northeast3", service="orders")
    for change in ({"observedGeneration": "2"}, {"reconciling": True},
                   {"terminalCondition": {"state": "CONDITION_FAILED"}}):
        saved = dict(service.state)
        service.state.update(change)
        assert adapter.observe()[0] is False, change
        service.state = saved
    assert adapter.observe()[0] is True


# -- Audit log collector ------------------------------------------------------------------------

from sova.adapters.gcp_audit_log_collector import GcpAuditLogCollector, classify, parse_time  # noqa: E402
from sova.domain.change_impact import ChangeKind  # noqa: E402


class Feed:
    def __init__(self):
        self.records, self.advances = [], []

    def record(self, change, *, collector_id):
        self.records.append(change)

    def advance(self, environment, *, collector_id, observed_through):
        self.advances.append((environment, observed_through))


def test_methods_are_classified_never_dropped():
    assert classify("storage.setIamPermissions") == ChangeKind.POLICY
    assert classify("google.iam.v1.IAMPolicy.SetIamPolicy") == ChangeKind.POLICY
    assert classify("cloudsql.instances.restoreBackup") == ChangeKind.RESOURCE
    assert classify("cloudsql.instances.update") == ChangeKind.UNCLASSIFIED
    assert classify(None) == ChangeKind.UNCLASSIFIED


def test_nanosecond_timestamps_are_read():
    assert parse_time("2026-11-15T07:00:01.123456789Z") == datetime(
        2026, 11, 15, 7, 0, 1, 123456, tzinfo=UTC)
    with pytest.raises(ContractError):
        parse_time("yesterday")


def test_successful_calls_are_recorded_once_and_the_watermark_lags():
    now = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
    entries = [
        {"insertId": "a1", "timestamp": "2026-11-15T07:58:00.5Z", "protoPayload": {
            "resourceName": "projects/_/buckets/orders", "methodName": "storage.setIamPermissions"}},
        {"insertId": "a2", "timestamp": "2026-11-15T07:58:30Z", "protoPayload": {
            "resourceName": "projects/_/buckets/orders", "methodName": "storage.buckets.update",
            "status": {"code": 7}}},  # refused: nothing changed
    ]
    bodies = []

    def entries_list(url, body, params):
        bodies.append(dict(body))
        return Response(200, {"entries": entries})

    feed = Feed()
    collector = GcpAuditLogCollector(
        feed, rest=rest({("POST", "https://logging.googleapis.com/v2/entries:list"):
                         entries_list}), clock=lambda: now, tenant_id="tenant-a",
        application_id="orders", collector_id="gcp-audit",
        resources={"projects/_/buckets/orders": ("gcp-source", "bucket-orders")})
    assert [c.kind for c in collector.poll()] == [ChangeKind.POLICY]
    assert collector.poll() == ()  # read again, recorded once
    assert len(feed.records) == 1 and feed.records[0].subject == "bucket-orders"
    assert feed.advances[0] == ("gcp-source", now - timedelta(minutes=10))
    assert 'protoPayload.resourceName:"projects/_/buckets/orders"' in bodies[0]["filter"]
    assert "cloudaudit.googleapis.com%2Factivity" in bodies[0]["filter"]


def test_entries_below_a_registered_resource_count_for_it_and_look_alikes_do_not():
    now = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
    secret = "projects/123/secrets/orders-db"
    entries = [
        {"insertId": "v1", "timestamp": "2026-11-15T07:58:00Z", "protoPayload": {
            "resourceName": secret + "/versions/2", "methodName":
            "google.cloud.secretmanager.v1.SecretManagerService.AddSecretVersion"}},
        {"insertId": "x1", "timestamp": "2026-11-15T07:58:01Z", "protoPayload": {
            "resourceName": secret + "-old", "methodName":
            "google.cloud.secretmanager.v1.SecretManagerService.DeleteSecret"}},
    ]
    feed = Feed()
    collector = GcpAuditLogCollector(
        feed, rest=rest({("POST", "https://logging.googleapis.com/v2/entries:list"):
                         Response(200, {"entries": entries})}), clock=lambda: now,
        tenant_id="tenant-a", application_id="orders", collector_id="gcp-audit",
        resources={secret: ("gcp-source", "secret-orders-db")})
    recorded = collector.poll()
    assert [(c.subject, c.kind) for c in recorded] == [("secret-orders-db", ChangeKind.RESOURCE)]
