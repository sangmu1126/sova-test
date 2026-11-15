"""Google Cloud REST access shared by the GCP adapters.

The adapters speak to the public REST APIs (the documented contract of each
service) through an authorized HTTP session: ``google.auth`` credentials with
``AuthorizedSession``, or anything with the same ``request`` method. Errors
carry the HTTP status and Google's reason (``NOT_FOUND``,
``FAILED_PRECONDITION``...), never a response body: provider messages can
quote request content, which for some adapters is secret.
"""

import re

from sova.domain.contracts import ContractError

_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"


class GcpApiError(ContractError):
    def __init__(self, method, url, status, reason):
        super().__init__(f"{method} {url.split('?', 1)[0]} -> {status} {reason}")
        self.status, self.reason = status, reason


def _reason(response):
    try:
        error = response.json().get("error", {})
    except ValueError:
        return "UNREADABLE"
    status = error.get("status")
    return status if isinstance(status, str) and re.fullmatch(r"[A-Z_]{1,64}", status) \
        else "UNKNOWN"


class GcpRest:
    def __init__(self, session, project, *, timeout=60):
        if not callable(getattr(session, "request", None)):
            raise ContractError("Authorized HTTP session required")
        if type(project) is not str or not _PROJECT.fullmatch(project):
            raise ContractError("Google Cloud project ID required")
        self.session, self.project, self.timeout = session, project, timeout

    @classmethod
    def default(cls, project, *, scopes=(CLOUD_PLATFORM,)):
        """Application default credentials, billed to ``project``."""
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=list(scopes), quota_project_id=project)
        return cls(AuthorizedSession(credentials), project)

    def call(self, method, url, *, json=None, params=None, missing_ok=False):
        response = self.session.request(method, url, json=json, params=params,
                                        timeout=self.timeout)
        if response.status_code == 404 and missing_ok:
            return None
        if not 200 <= response.status_code < 300:
            raise GcpApiError(method, url, response.status_code, _reason(response))
        if not response.content:
            return {}
        return response.json()

    def get(self, url, **kwargs):
        return self.call("GET", url, **kwargs)

    def post(self, url, body=None, **kwargs):
        return self.call("POST", url, json=body if body is not None else {}, **kwargs)

    def patch(self, url, body, **kwargs):
        return self.call("PATCH", url, json=body, **kwargs)

    def delete(self, url, **kwargs):
        return self.call("DELETE", url, **kwargs)

    def pages(self, url, key, *, params=None, limit=100_000):
        """Every item of a paged list (``nextPageToken``), bounded."""
        params, items = dict(params or {}), []
        while True:
            page = self.get(url, params=params) or {}
            items += page.get(key, [])
            if len(items) > limit:
                raise ContractError("More items than the bound")
            token = page.get("nextPageToken")
            if not token:
                return items
            params["pageToken"] = token
