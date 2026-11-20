"""Disposable real IdP for the native browser journey, never a customer IdP.

Passwords are random per run and enter the browser only on private stdin.
Only an explicitly supplied, cached image is accepted. No shared container,
realm, account, kubeconfig or cloud resource is changed.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from uuid import uuid4

import httpx
from keycloak_fixtures import Server
from qualification_private_files import private_file

IMAGE = "sha256:923a431c08971d49727a857e64870d8d63b5bf294a730e5dde729b5138c26c9e"
OPTIMIZED_IMAGE = "sha256:9ff9180b6120c45f43f3bb4b9706eaf5fc250115581ee7c5cee067500f727e5c"
CLIENT = "sova-workspace"


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
    if result.returncode:
        # Do not include an environment file or arbitrary server output.
        raise RuntimeError("Run-owned identity container operation failed")
    return result.stdout.strip()


@contextmanager
def identity_provider(h, *, startup_seconds=100, security_roles=False):
    if type(startup_seconds) is not int or not 30 <= startup_seconds <= 900:
        raise ValueError("Explicit bounded local IdP cold-start budget required")
    image = os.environ.get("SOVA_TEST_JOURNEY_KEYCLOAK_IMAGE")
    if image not in (IMAGE, OPTIMIZED_IMAGE):
        raise ValueError("The explicitly selected Keycloak 26.8.2 arm64 image is required")
    measured = json.loads(docker("image", "inspect", image))[0]
    if measured["Id"] != image or measured["Architecture"] != "arm64":
        raise ValueError("Identity image differs from the measured native image")
    optimized = image == OPTIMIZED_IMAGE
    if optimized:
        base = json.loads(docker("image", "inspect", IMAGE))[0]
        layers = base["RootFS"]["Layers"]
        if (measured["Config"]["User"] != "1000"
                or measured["RootFS"]["Layers"][:-1] != layers
                or measured["Config"]["Labels"].get("sova.keycloak.base") != IMAGE
                or measured["Config"]["Labels"].get("sova.purpose") != "disposable-qualification-idp"):
            raise ValueError("Optimized test identity image must extend exactly the pinned upstream layers")
    run = "sova-journey-oidc-" + uuid4().hex
    setup = os.environ.get("SOVA_TEST_JOURNEY_IDENTITY_SETUP", "admin-api")
    if setup not in ("admin-api", "realm-import") or (setup == "realm-import" and not optimized):
        raise ValueError("Realm import requires the pinned optimized test image")
    imported = setup == "realm-import"
    realm = users = document = None
    if imported:
        from qualification_imported_identity import definition

        realm, users, document = definition(h, client_id=CLIENT, security_roles=security_roles)
    password = secrets.token_urlsafe(32)
    env_file = private_file(h.directory / "keycloak.env", "\n".join((
        *(("KC_BOOTSTRAP_ADMIN_USERNAME=run-admin", "KC_BOOTSTRAP_ADMIN_PASSWORD=" + password) if not imported else ()),
        "KC_HOSTNAME_STRICT=false", "JAVA_OPTS_KC_HEAP=-Xms128m -Xmx640m")))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    container = None
    network_created = False
    report = {"run_id": run, "image_id": image, "architecture": "arm64", "version": "26.8.2",
              "base_image_id": IMAGE, "prebuilt_augmentation": optimized, "realm_setup": setup,
              "transport": "loopback HTTP; local test only", "operational_idp_qualified": False,
              "direct_password_grant_for_spa": False, "pkce_method": "S256"}
    try:
        # Docker Desktop does not publish a port on an internal bridge. Use a
        # separate bridge with an explicit loopback-only published address.
        docker("network", "create", "--label", "sova.journey=" + run, run)
        network_created = True
        start = ["start", "--optimized", "--http-enabled=true", "--hostname-strict=false", "--cache=local"] if optimized else ["start-dev"]
        if imported:
            from qualification_imported_identity import WAIT_AND_START

            start = ["-ec", WAIT_AND_START]
        entrypoint = ["--entrypoint", "/bin/bash"] if imported else []
        container = docker("create", "--name", run, "--network", run,
            "--label", "sova.journey=" + run, "--pull=never", "--memory", "1200m",
            "--cpus", "2", "--pids-limit", "256", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--env-file", str(env_file),
            "--tmpfs", "/opt/keycloak/data:rw,size=192m,uid=1000,gid=0,mode=0700",
            "-p", f"127.0.0.1:{port}:8080", *entrypoint, image, *start)
        docker("start", container)
        if imported:
            from qualification_imported_identity import transfer

            transfer(container, realm, document)
            # Do not retain password-bearing realm input after transfer.
            document = None
        started = time.monotonic()
        deadline = started + startup_seconds
        with httpx.Client(timeout=2, trust_env=False) as client:
            while True:
                try:
                    response = client.get(origin + "/realms/" + (realm if imported else "master") + "/.well-known/openid-configuration")
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Run-owned Keycloak did not become ready")
                time.sleep(0.5)
        report["cold_start_seconds"] = round(time.monotonic() - started, 3)
        report["cold_start_budget_seconds"] = startup_seconds
        if imported:
            report["administrator_token_issues"] = 0
            report["administrator_token_cached_with_server_expiry"] = False
            report["realm_secret_transport"] = "private stdin to owned non-root container tmpfs"
        else:
            server = Server(origin, ("run-admin", password))
            realm = "journey-" + uuid4().hex
            server.call("POST", "", {"realm": realm, "enabled": True,
                "registrationAllowed": False, "resetPasswordAllowed": False,
                "accessTokenLifespan": 120, "ssoSessionIdleTimeout": 600})
            mappers = [
                {"name": "tenant", "protocol": "openid-connect", "protocolMapper": "oidc-hardcoded-claim-mapper",
                 "config": {"claim.name": "sova_tenant", "claim.value": h.tenant,
                    "jsonType.label": "String", "access.token.claim": "true", "id.token.claim": "true"}},
                {"name": "api-audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
                 "config": {"included.custom.audience": "sova-control-plane",
                    "access.token.claim": "true", "id.token.claim": "false"}},
            ]
            server.call("POST", f"/{realm}/clients", {"clientId": CLIENT, "publicClient": True,
                "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
                "implicitFlowEnabled": False, "serviceAccountsEnabled": False,
                "redirectUris": [h.origin + "/workspace/callback"], "webOrigins": [h.origin],
                "attributes": {"pkce.code.challenge.method": "S256"}, "protocolMappers": mappers})
            roles = ("registrar", "attestor", "planner", "operator", "approver", "auditor")
            security = ("trust_requester", "trust_approver") if security_roles else ()
            for role in (*roles, *security):
                server.call("POST", f"/{realm}/roles", {"name": "sova:" + role})
            users = {}
            for name, granted in (("planner", (*roles, *(("trust_requester",) if security_roles else ()))),
                                  ("approver", ("approver", "auditor", *(("trust_approver",) if security_roles else ())))):
                secret = secrets.token_urlsafe(24)
                uid = server.user(realm, name, secret)
                mapped = [server.call("GET", f"/{realm}/roles/sova:{r}") for r in granted]
                server.call("POST", f"/{realm}/users/{uid}/role-mappings/realm", mapped)
                users[name] = {"username": name, "password": secret, "subject": uid}
            report["administrator_token_issues"] = server.admin_token_issues
            report["administrator_token_cached_with_server_expiry"] = True
        report["dedicated_security_roles"] = security_roles
        issuer = origin + "/realms/" + realm
        h.environment.update(SOVA_OIDC_ISSUER=issuer,
            SOVA_OIDC_JWKS_URL=issuer + "/protocol/openid-connect/certs",
            SOVA_CONSOLE_ROOT=str(Path(__file__).resolve().parents[1] / "console/dist"),
            SOVA_CONSOLE_ORIGIN=h.origin, SOVA_CONSOLE_CLIENT_ID=CLIENT,
            SOVA_CONSOLE_TENANT=h.tenant)
        yield {"origin": origin, "issuer": issuer, "users": users, "report": report}
    finally:
        if container:
            # Keep private startup/runtime diagnostics before owned cleanup.
            # Never persist inspect Config/Env, which contains bootstrap credentials.
            state = json.loads(docker("inspect", container))[0]["State"]
            (h.directory / "keycloak-state.json").write_text(json.dumps({
                key: state.get(key) for key in ("Status", "Running", "OOMKilled", "ExitCode", "Error", "StartedAt", "FinishedAt")
            }, indent=2))
            (h.directory / "keycloak.log").write_text(docker("logs", "--tail", "120", container))
            # ID and immutable run label, not a broad name glob, establish ownership.
            info = json.loads(docker("inspect", container))[0]
            if info["Config"]["Labels"].get("sova.journey") != run:
                raise RuntimeError("Identity container ownership changed; refusing cleanup")
            docker("rm", "-f", container)
            report["container_removed"] = container not in docker("ps", "-aq", "--no-trunc").splitlines()
        if network_created:
            docker("network", "rm", run)
            report["network_removed"] = run not in docker("network", "ls", "--format", "{{.Name}}").splitlines()
        env_file.unlink(missing_ok=True)
        report["bootstrap_environment_removed"] = not env_file.exists()
        private_file(h.directory / ("identity-" + run + ".json"), json.dumps(report, indent=2))
        artifacts = os.environ.get("SOVA_TEST_QUALIFICATION_ARTIFACTS")
        if artifacts:
            private_file(Path(artifacts) / (h.case_name + "-identity.json"), json.dumps(report, indent=2))
