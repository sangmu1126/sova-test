"""Is the platform really locked out of the environment it handed over? (§20.2)

Each probe uses one credential the platform held and asks the environment
itself whether it still works. The answer is three-valued:

* ``True`` - the environment refused the credential (an authentication or
  authorization refusal it reported);
* ``False`` - the credential still works: the platform is not out;
* ``None`` - nothing could be decided (unreachable, timed out, an unexpected
  error). An unreachable endpoint proves nothing about revocation, so it is
  never counted as a refusal.

Observations name the surface and the refusal, never the credential.
"""

import psycopg

from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.domain.contracts import ContractError

_PG_REFUSALS = ("password authentication failed", "is not permitted to log in",
                "permission denied", "no pg_hba.conf entry")


def kubernetes_token(request):
    """The platform's service-account token: ``request()`` makes one request
    the platform was allowed to make (through its registered controls)."""
    try:
        request()
        return False, {"surface": "kubernetes", "outcome": "accepted"}
    except KubernetesRequestRefused as refused:
        if refused.status in (401, 403):
            return True, {"surface": "kubernetes", "outcome": f"refused {refused.status}"}
        return None, {"surface": "kubernetes", "outcome": f"status {refused.status}"}
    except ContractError:
        return None, {"surface": "kubernetes", "outcome": "undecided"}


def postgres_role(connect):
    """The platform's database role: ``connect()`` opens a connection as it."""
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
        return False, {"surface": "postgresql", "outcome": "accepted"}
    except psycopg.OperationalError as error:
        text = str(error).lower()
        role_gone = 'role "' in text and "does not exist" in text
        if role_gone or any(reason in text for reason in _PG_REFUSALS):
            return True, {"surface": "postgresql", "outcome": "refused"}
        return None, {"surface": "postgresql", "outcome": "undecided"}


def oidc_admin(http, token_url, *, username, password, client_id="admin-cli"):
    """The platform's identity-provider administrator, by a token request."""
    try:
        response = http.post(token_url, data={"grant_type": "password",
                                               "client_id": client_id,
                                               "username": username, "password": password})
    except Exception:  # transport failures decide nothing
        return None, {"surface": "identity-provider", "outcome": "undecided"}
    if response.status_code == 200:
        return False, {"surface": "identity-provider", "outcome": "accepted"}
    if response.status_code in (400, 401, 403):
        return True, {"surface": "identity-provider",
                      "outcome": f"refused {response.status_code}"}
    return None, {"surface": "identity-provider", "outcome": f"status {response.status_code}"}


def s3_account(client, bucket):
    """The platform's object-storage account, by reading the handed-over bucket."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        client.list_objects_v2(Bucket=bucket, MaxKeys=1)
        return False, {"surface": "object-storage", "outcome": "accepted"}
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch",
                    "AllAccessDisabled"):
            return True, {"surface": "object-storage", "outcome": f"refused {code}"}
        return None, {"surface": "object-storage", "outcome": "undecided"}
    except BotoCoreError:
        return None, {"surface": "object-storage", "outcome": "undecided"}


def platform_exit(lockouts, operations):
    """The ``exit.platform.customer_operates_after_revocation`` drill.

    ``lockouts`` and ``operations`` map names to zero-argument measurements:
    lockouts return ``(refused, observation)`` as above, operations return
    ``(succeeded, observation)`` for what the customer did with its own
    credentials only. The drill passes only if every platform credential was
    refused and every customer operation succeeded; one undecided lockout is
    enough to withhold the pass."""
    if not lockouts or not operations:
        raise ContractError("Platform credentials and customer operations required")
    locked = {name: probe() for name, probe in sorted(lockouts.items())}
    done = {name: run() for name, run in sorted(operations.items())}
    passed = all(r is True for r, _ in locked.values()) and all(
        ok is True for ok, _ in done.values())
    return passed, {"probe": "customer_operates_after_revocation",
                    "platform_refused": {n: o for n, (_, o) in locked.items()},
                    "customer_operations": {n: o for n, (_, o) in done.items()}}
