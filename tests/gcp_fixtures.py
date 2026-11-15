"""Opt-in fixtures for tests against a real Google Cloud project.

Nothing here runs unless ``SOVA_TEST_GCP_LIVE=1`` names the intent and
``SOVA_TEST_GCP_PROJECT``, ``SOVA_TEST_GCP_REGION`` and ``SOVA_TEST_GCP_RUN``
are set; ``infra/cloud-env/cloud-env.sh env`` prints them for the current run.
Every resource carries ``sova-test`` and the run id in its name (and a
``sova-test`` label where the service has labels), so ``cloud-env.sh down``
removes exactly what a run created. Credentials are the local gcloud login
and are never printed.
"""

import base64
import json
import os
import re
import secrets
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from uuid import uuid4

import pytest

from managed_postgres import managed_business


def gcloud(gcp, *args, check=True):
    result = subprocess.run(["gcloud", *args, "--project", gcp["project"], "--format", "json",
                             "--quiet"], capture_output=True, text=True, timeout=1800)
    if result.returncode:
        if check:
            raise AssertionError(f"gcloud {' '.join(args[:3])}: {result.stderr.strip()[-400:]}")
        return None
    return json.loads(result.stdout) if result.stdout.strip() else {}


@pytest.fixture(scope="session")
def gcp():
    if os.environ.get("SOVA_TEST_GCP_LIVE") != "1":
        pytest.skip("SOVA_TEST_GCP_LIVE=1 is required for tests against a real GCP project")
    run = os.environ.get("SOVA_TEST_GCP_RUN", "")
    if not re.fullmatch(r"[a-z0-9]{4,8}", run):
        raise pytest.UsageError("SOVA_TEST_GCP_RUN must be a short lowercase run id")
    return {"project": os.environ["SOVA_TEST_GCP_PROJECT"],
            "region": os.environ["SOVA_TEST_GCP_REGION"], "run": run,
            "labels": {"sova-test": run}}


def primary_ip(described):
    return next(a["ipAddress"] for a in described.get("ipAddresses", [])
                if a.get("type") == "PRIMARY")


@pytest.fixture(scope="session")
def cloudsql(gcp):
    """This run's Cloud SQL instance, checked against what the DSN reaches."""
    instance = os.environ.get("SOVA_TEST_CLOUDSQL_ID", "")
    dsn = os.environ.get("SOVA_TEST_CLOUDSQL_DSN", "")
    if not dsn:
        pytest.skip("SOVA_TEST_CLOUDSQL_DSN is required (cloud-env.sh env)")
    if instance != f"sova-test-pg-{gcp['run']}":
        raise pytest.UsageError("Only this run's Cloud SQL instance may be used")
    described = gcloud(gcp, "sql", "instances", "describe", instance)
    ip = primary_ip(described)
    if not re.search(rf"(?:hostaddr|host)={re.escape(ip)}(?:\s|$)", dsn):
        raise pytest.UsageError("SOVA_TEST_CLOUDSQL_DSN does not reach this run's instance")
    return {"instance": instance, "dsn": dsn, "ip": ip, "described": described,
            "dns_name": described.get("dnsName") or "",
            "role_password": os.environ["SOVA_TEST_CLOUDSQL_ROLE_PASSWORD"],
            "root_password": os.environ["SOVA_TEST_CLOUDSQL_ROOT_PASSWORD"]}


@pytest.fixture
def cloudsql_business(cloudsql, postgres_database):
    return managed_business(postgres_database[0], cloudsql["dsn"], cloudsql["role_password"])


POD_CA = "/tmp/sova-cloud/ca.pem"


def conninfo_value(dsn, key):
    found = re.search(rf"(?:^|\s){key}=(\S+)", dsn)
    return found.group(1) if found else None


def subscription_channel(business, dsn, *, user, password, pod_ca):
    """The business world with a subscription channel from the local server to a
    managed source: the server itself connects out over verified TLS, so the
    source's CA goes into the server's pod and the channel names it there."""
    if not os.environ.get("SOVA_TEST_POSTGRES_EXEC"):
        pytest.skip("SOVA_TEST_POSTGRES_EXEC is required to give the local server the CA")
    ca = conninfo_value(dsn, "sslrootcert")
    exec_ = shlex.split(os.environ["SOVA_TEST_POSTGRES_EXEC"])
    with open(ca, "rb") as handle:
        subprocess.run([*exec_, "sh", "-c", f"mkdir -p {os.path.dirname(pod_ca)} && "
                        f"cat > {pod_ca}"], input=handle.read(), check=True, timeout=60)
    host, hostaddr = conninfo_value(dsn, "host"), conninfo_value(dsn, "hostaddr")

    def channel(database):
        parts = {"host": host, "port": "5432", "user": user, "password": password,
                 "dbname": database, "sslmode": "verify-full", "sslrootcert": pod_ca}
        if hostaddr:
            parts["hostaddr"] = hostaddr
        return " ".join(f"{k}={v}" for k, v in parts.items())

    return {**business, "replication_conninfo": channel}


def logical_source(business, grant):
    with business["connect"](business["names"]["src"]) as conn:
        if conn.execute("SHOW wal_level").fetchone()[0] != "logical":
            pytest.skip("the managed source runs without logical decoding")
        conn.execute(grant)
        conn.commit()


@pytest.fixture
def cloudsql_cdc(cloudsql, cloudsql_business):
    logical_source(cloudsql_business, "ALTER ROLE postgres WITH REPLICATION")
    return subscription_channel(cloudsql_business, cloudsql["dsn"], user="postgres",
                                password=cloudsql["root_password"], pod_ca=POD_CA)


@pytest.fixture
def rds_cdc(aws, cloud_business):
    """RDS as a logical source: ``rds.logical_replication`` in the run's
    parameter group; the master user replicates through ``rds_replication``."""
    dsn = cloud_business["servers"][cloud_business["names"]["src"]]
    logical_source(cloud_business, "GRANT rds_replication TO CURRENT_USER")
    return subscription_channel(cloud_business, dsn, user=conninfo_value(dsn, "user"),
                                password=os.environ["SOVA_TEST_RDS_MASTER_PASSWORD"],
                                pod_ca="/tmp/sova-cloud/rds-ca.pem")


# -- REST access and the run-scoped resources of the other services -----------------------

SECRETS_API = "https://secretmanager.googleapis.com/v1"
DNS_API = "https://dns.googleapis.com/dns/v1"
BUSYBOX = "docker.io/library/busybox:1.37.0"


def suffix():
    return uuid4().hex[:6]


@pytest.fixture(scope="session")
def gcp_rest(gcp):
    pytest.importorskip("google.auth")
    from sova.adapters.gcp_rest import GcpRest

    return GcpRest.default(gcp["project"])


def add_version(gcp_rest, name, value):
    from sova.adapters.gcp_secret_manager import crc32c

    gcp_rest.post(f"{SECRETS_API}/projects/{gcp_rest.project}/secrets/{name}:addVersion", {
        "payload": {"data": base64.b64encode(value).decode(), "dataCrc32c": str(crc32c(value))}})


@pytest.fixture
def gcp_secrets(gcp, gcp_rest):
    """Two run-scoped secrets with random values, kept in the run's region."""
    base = f"sova-test-{gcp['run']}-{suffix()}"
    names = {"db": f"{base}-orders-db", "api": f"{base}-orders-api-key"}
    values = {"db": json.dumps({"username": "orders",
                                "password": secrets.token_urlsafe(24)}).encode(),
              "api": secrets.token_bytes(32)}
    created = []
    try:
        for key, name in names.items():
            gcp_rest.post(f"{SECRETS_API}/projects/{gcp['project']}/secrets", {
                "replication": {"userManaged": {"replicas": [{"location": gcp["region"]}]}},
                "labels": gcp["labels"]}, params={"secretId": name})
            created.append(name)
            add_version(gcp_rest, name, values[key])
        yield {"rest": gcp_rest, "names": names, "values": values,
               "add_version": lambda key, value: add_version(gcp_rest, names[key], value)}
    finally:
        for name in created:
            gcp_rest.delete(f"{SECRETS_API}/projects/{gcp['project']}/secrets/{name}",
                            missing_ok=True)


def identity_token():
    return subprocess.run(["gcloud", "auth", "print-identity-token"], capture_output=True,
                          text=True, check=True, timeout=60).stdout.strip()


def run_answers(url, token):
    """True only if the source application itself answers at ``url`` (the
    service is private: the call carries this user's identity token)."""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {identity_token()}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return token in response.read(4096).decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False


@pytest.fixture
def cloud_run_app(gcp):
    """A private Cloud Run service that answers with a run-unique token."""
    name = f"sova-test-orders-{gcp['run']}-{suffix()}"
    token = f"sova-{gcp['run']}-{secrets.token_hex(8)}"
    try:
        subprocess.run([
            "gcloud", "run", "deploy", name, "--project", gcp["project"], "--region",
            gcp["region"], f"--image={BUSYBOX}", "--no-allow-unauthenticated", "--port=8080",
            "--command=sh", f"--args=-c,mkdir -p /www && echo {token} > /www/index.html"
            " && exec httpd -f -p 8080 -h /www", f"--labels=sova-test={gcp['run']}",
            "--max-instances=1", "--memory=256Mi", "--cpu=1", "--quiet"],
            capture_output=True, text=True, check=True, timeout=900)
        described = gcloud(gcp, "run", "services", "describe", name, "--region", gcp["region"])
        yield {"name": name, "token": token, "region": gcp["region"],
               "url": described["status"]["url"]}
    finally:
        gcloud(gcp, "run", "services", "delete", name, "--region", gcp["region"], check=False)


@pytest.fixture
def dns_zone(gcp, gcp_rest):
    """A public managed zone for a name nobody delegates to it: only its own
    name servers answer for it, which is all a route store needs."""
    name = f"sova-test-{gcp['run']}-{suffix()}"
    url = f"{DNS_API}/projects/{gcp['project']}/managedZones"
    created = gcp_rest.post(url, {"name": name, "dnsName": f"{name}.com.",
                                  "description": "sova test, run-scoped",
                                  "visibility": "public", "labels": gcp["labels"]})
    try:
        yield {"rest": gcp_rest, "zone": name, "name": f"{name}.com",
               "servers": created["nameServers"]}
    finally:
        records = gcp_rest.get(f"{url}/{name}/rrsets").get("rrsets", [])
        doomed = [r for r in records if r["type"] not in ("NS", "SOA")]
        if doomed:
            gcp_rest.post(f"{url}/{name}/changes", {"deletions": doomed})
        gcp_rest.delete(f"{url}/{name}", missing_ok=True)


# -- Identity Platform -------------------------------------------------------------------

IDT = "https://identitytoolkit.googleapis.com"


@pytest.fixture(scope="session")
def identity_platform(gcp, gcp_rest):
    """Identity Platform with tenants, and a sign-in API key limited to it.

    Turning Identity Platform on is a one-way upgrade of the project (it
    cannot go back to Firebase Authentication); the test project is
    dedicated to these runs. The key exists only for this session."""
    p = gcp["project"]
    config = gcp_rest.get(f"{IDT}/v2/projects/{p}/config", missing_ok=True)
    if config is None:
        gcp_rest.post(f"{IDT}/v2/projects/{p}/identityPlatform:initializeAuth")
        config = gcp_rest.get(f"{IDT}/v2/projects/{p}/config")
    if not (config.get("multiTenant") or {}).get("allowTenants"):
        gcp_rest.patch(f"{IDT}/v2/projects/{p}/config", {"multiTenant": {"allowTenants": True}},
                       params={"updateMask": "multiTenant.allowTenants"})
    created = gcloud(gcp, "services", "api-keys", "create",
                     f"--display-name=sova-test-{gcp['run']}-signin",
                     "--api-target=service=identitytoolkit.googleapis.com")
    key = created.get("response", created)
    name = key["name"]
    try:
        value = key.get("keyString") or gcloud(gcp, "services", "api-keys", "get-key-string",
                                               name)["keyString"]
        yield {"key": value, "rest": gcp_rest, "project": p}
    finally:
        # A key used in the last 7 days is protected from deletion unless the check is
        # skipped (measured); this one is the session's own.
        gcloud(gcp, "services", "api-keys", "delete", name, "--no-check-existing-usage",
               check=False)


def idp_password():
    return "Sv1!" + secrets.token_urlsafe(18)


@pytest.fixture
def idp_tenant(gcp, identity_platform):
    """A run-scoped tenant with synthetic users: alice (finance), bob
    (finance, support) and carol (disabled), each with a real password."""
    rest, p = identity_platform["rest"], gcp["project"]
    tenant = rest.post(f"{IDT}/v2/projects/{p}/tenants", {
        "displayName": f"sova-test-{gcp['run']}", "allowPasswordSignup": True})
    tid = tenant["name"].rsplit("/", 1)[-1]
    accounts = f"{IDT}/v1/projects/{p}/tenants/{tid}/accounts"
    try:
        passwords, uids = {}, {}
        for name, groups, enabled in (("alice", ["finance"], True),
                                      ("bob", ["finance", "support"], True),
                                      ("carol", [], False)):
            passwords[name] = idp_password()
            uids[name] = rest.post(accounts, {
                "email": f"{name}@example.test", "password": passwords[name],
                "displayName": f"{name.title()} Tester", "emailVerified": True})["localId"]
            rest.post(accounts + ":update", {
                "localId": uids[name], "customAttributes": json.dumps({"groups": groups}),
                "disableUser": not enabled})
        from sova.adapters.gcp_identity_platform_directory import IdentityPlatformDirectory

        yield {"rest": rest, "tenant": tid, "tenant_url": f"{IDT}/v2/{tenant['name']}",
               "accounts": accounts, "passwords": passwords, "uids": uids,
               "key": identity_platform["key"], "project": p,
               "directory": IdentityPlatformDirectory(rest, tenant=tid)}
    finally:
        rest.delete(f"{IDT}/v2/{tenant['name']}", missing_ok=True)


def idp_login(tenant, email, password):
    """Identity Platform has no password grant at an OIDC token endpoint; its
    REST sign-in (with the tenant) is what a client calls."""
    request = urllib.request.Request(
        f"{IDT}/v1/accounts:signInWithPassword?key={tenant['key']}",
        data=json.dumps({"email": email, "password": password, "returnSecureToken": True,
                         "tenantId": tenant["tenant"]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read()), None
    except urllib.error.HTTPError as error:
        message = json.loads(error.read() or b"{}").get("error", {}).get("message", "")
        return None, message.split(" ", 1)[0] or str(error.code)


# -- Cloud Storage: two writer service accounts the test acts as -----------------------


def _account(gcp, writer):
    return f"sova-test-{writer}-{gcp['run']}@{gcp['project']}.iam.gserviceaccount.com"


@pytest.fixture(scope="session")
def gcs_writers(gcp, gcp_rest):
    """``app`` and ``other`` as run-scoped service accounts holding nothing of
    their own; bucket grants decide. The test may act as them (token creator
    on each, test-only: the adapter never impersonates a writer)."""
    import google.auth
    from google.auth import impersonated_credentials
    from google.auth.transport.requests import AuthorizedSession, Request

    from sova.adapters.gcp_rest import CLOUD_PLATFORM, GcpRest

    me = subprocess.run(["gcloud", "config", "get-value", "account"], capture_output=True,
                        text=True, check=True).stdout.strip()
    source, _ = google.auth.default(scopes=[CLOUD_PLATFORM], quota_project_id=gcp["project"])
    rests, members = {}, {}
    for writer, name in (("app", "app"), ("other", "batch")):
        email = _account(gcp, name)
        if gcloud(gcp, "iam", "service-accounts", "describe", email, check=False) is None:
            gcloud(gcp, "iam", "service-accounts", "create", f"sova-test-{name}-{gcp['run']}",
                   f"--display-name=sova test writer {name} {gcp['run']}")
        for _ in range(12):  # a new account is visible to IAM a little later
            if gcloud(gcp, "iam", "service-accounts", "add-iam-policy-binding", email,
                      f"--member=user:{me}", "--role=roles/iam.serviceAccountTokenCreator",
                      check=False) is not None:
                break
            time.sleep(5)
        credentials = impersonated_credentials.Credentials(
            source_credentials=source, target_principal=email, target_scopes=[CLOUD_PLATFORM],
            lifetime=3600)
        rests[writer] = GcpRest(AuthorizedSession(credentials), gcp["project"])
        members[writer] = f"serviceAccount:{email}"
    deadline = time.monotonic() + 120
    while True:  # the token creator grant reaches IAM a little later
        try:
            for rest in rests.values():
                rest.session.credentials.refresh(Request())
            break
        except Exception:  # noqa: BLE001 - retried until the grant is visible
            if time.monotonic() > deadline:
                raise
            time.sleep(5)
    return {"as": rests, "members": members}


@pytest.fixture
def gcs_stores(gcp, gcp_rest, gcs_writers):
    return {"admin": gcp_rest, "as": gcs_writers["as"], "members": gcs_writers["members"],
            "region": gcp["region"], "project": gcp["project"], "run": gcp["run"]}


# -- between the two clouds ---------------------------------------------------------------


@pytest.fixture(params=["aws-to-gcp", "gcp-to-aws"])
def cross_cloud_business(request, aws, cloudsql, postgres_database):
    """The business world with its source in one cloud and its target, restore
    scratch and other tenant in the other: RDS and Cloud SQL of this run."""
    rds, password = os.environ.get("SOVA_TEST_RDS_DSN"), os.environ.get(
        "SOVA_TEST_RDS_ROLE_PASSWORD")
    if not rds or not password:
        pytest.skip("SOVA_TEST_RDS_DSN and SOVA_TEST_RDS_ROLE_PASSWORD are required")
    if f"sova-test-pg-{aws['run']}." not in rds:
        raise pytest.UsageError("Only this run's RDS instance may be used")
    source, target = ((rds, cloudsql["dsn"]) if request.param == "aws-to-gcp"
                      else (cloudsql["dsn"], rds))
    business = managed_business(postgres_database[0], source, password, target=target)
    return {**business, "direction": request.param}
