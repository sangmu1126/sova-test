"""Two disposable Keycloak servers for identity migration tests.

``SOVA_TEST_KEYCLOAK_URLS`` names the source and target servers (comma
separated) and ``SOVA_TEST_KEYCLOAK_ADMIN`` the bootstrap ``user:password`` of
both. Only loopback servers are accepted, so these tests can never reach a
real identity provider. Every test creates its own realms.
"""

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest

CLIENT = "orders-web"


class Server:
    def __init__(self, url, admin):
        import httpx

        self.url, self.admin, self.httpx = url.rstrip("/"), admin, httpx

    def _client(self):
        http = self.httpx.Client(timeout=15)
        token = http.post(f"{self.url}/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli",
            "username": self.admin[0], "password": self.admin[1]})
        token.raise_for_status()
        http.headers["Authorization"] = "Bearer " + token.json()["access_token"]
        return http

    def call(self, method, path, body=None, **params):
        http = self._client()
        try:
            response = http.request(method, f"{self.url}/admin/realms{path}", json=body,
                                    params=params)
            response.raise_for_status()
        finally:
            http.close()
        return response.json() if response.content and response.headers.get(
            "content-type", "").startswith("application/json") else response

    def realm(self, name):
        """A realm with a test client that may sign users in directly and puts
        group membership into its tokens."""
        self.call("POST", "", {"realm": name, "enabled": True})
        self.call("POST", f"/{name}/clients", {
            "clientId": CLIENT, "publicClient": True, "directAccessGrantsEnabled": True,
            "standardFlowEnabled": False,
            "protocolMappers": [{
                "name": "groups", "protocol": "openid-connect",
                "protocolMapper": "oidc-group-membership-mapper",
                "config": {"full.path": "false", "id.token.claim": "true",
                           "access.token.claim": "true", "claim.name": "groups",
                           "userinfo.token.claim": "true"}}]})
        return f"{self.url}/realms/{name}"

    def user(self, realm, username, password, *, groups=(), enabled=True):
        location = self.call("POST", f"/{realm}/users", {
            "username": username, "email": f"{username}@example.test",
            "firstName": username.title(), "lastName": "Tester", "enabled": enabled,
            "emailVerified": True,
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        }).headers["Location"]
        user_id = location.rstrip("/").rsplit("/", 1)[1]
        existing = {g["path"]: g["id"] for g in self.call("GET", f"/{realm}/groups")}
        for group in groups:
            if "/" + group not in existing:
                self.call("POST", f"/{realm}/groups", {"name": group})
                existing = {g["path"]: g["id"] for g in self.call("GET", f"/{realm}/groups")}
            self.call("PUT", f"/{realm}/users/{user_id}/groups/{existing['/' + group]}")
        return user_id

    def find(self, realm, username):
        found = self.call("GET", f"/{realm}/users", username=username, exact="true",
                          briefRepresentation="false")
        return found[0] if found else None


@pytest.fixture(scope="session")
def keycloak_servers():
    urls = os.environ.get("SOVA_TEST_KEYCLOAK_URLS")
    if not urls:
        pytest.skip("SOVA_TEST_KEYCLOAK_URLS is required for real identity-provider tests")
    pytest.importorskip("httpx")
    parsed = [u.strip() for u in urls.split(",") if u.strip()]
    if len(parsed) != 2 or any(urlparse(u).hostname not in ("127.0.0.1", "localhost", "::1")
                               for u in parsed):
        raise pytest.UsageError("Identity tests run only against two loopback servers")
    user, _, password = os.environ["SOVA_TEST_KEYCLOAK_ADMIN"].partition(":")
    return {"source": Server(parsed[0], (user, password)),
            "target": Server(parsed[1], (user, password)),
            "admin": (user, password)}


@pytest.fixture
def realm_name():
    return "orders-" + uuid4().hex[:10]
