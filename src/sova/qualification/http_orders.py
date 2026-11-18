"""Fixed-origin order requests, journalled before each real HTTP transmission."""

import ipaddress
import json
from urllib.parse import urlsplit

import httpx

from sova.qualification.requests import OrderIntent, receipt_matches


class OrderHttpClient:
    def __init__(self, origin, *, bearer_token, journal, timeout=5, client=None):
        url = urlsplit(origin)
        if (url.scheme not in ("https", "http") or not url.hostname or url.username
                or url.password or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError("A fixed HTTP origin without credentials is required")
        if url.scheme == "http":
            try:
                loopback = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                loopback = url.hostname == "localhost"
            if not loopback:
                raise ValueError("Plain HTTP is allowed only for loopback tests")
        if not isinstance(bearer_token, str) or not bearer_token or any(
                c.isspace() for c in bearer_token):
            raise ValueError("A bearer token is required")
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError("A bounded request timeout is required")
        self.journal = journal
        self.origin = origin.rstrip("/")
        self.token = bearer_token
        self._owned = client is None
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)

    def close(self):
        if self._owned:
            self.client.close()

    def submit(self, intent):
        return self._request(intent, "submit")

    def lookup(self, intent):
        return self._request(intent, "lookup")

    def _request(self, intent, kind):
        if not isinstance(intent, OrderIntent):
            raise ValueError("A typed order intent is required")
        self.journal.request(intent)
        attempt = self.journal.begin(intent.request_id, kind=kind)
        headers = {"authorization": "Bearer " + self.token,
                   "x-qualification-tenant": self.journal.scope["tenant_id"],
                   "x-qualification-run": self.journal.scope["run_id"],
                   "idempotency-key": intent.idempotency_key}
        try:
            kwargs = {"json": {"customer": intent.customer, "amount": intent.amount}} if kind == "submit" else {
                "params": {"customer": intent.customer, "idempotency_key": intent.idempotency_key}}
            with self.client.stream("POST" if kind == "submit" else "GET", self.origin + "/orders",
                                    headers=headers, follow_redirects=False, **kwargs) as response:
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    data.extend(chunk)
                    if len(data) > 16384:
                        break
                receipt = None
                error = None
                if 200 <= response.status_code < 300:
                    try:
                        parsed = json.loads(data) if len(data) <= 16384 else None
                    except (ValueError, UnicodeError):
                        parsed = None
                    if receipt_matches(parsed, {"customer": intent.customer,
                                               "idempotency_key": intent.idempotency_key,
                                               "amount": intent.amount}):
                        receipt = parsed
                    else:
                        error = "invalid_receipt"
                self.journal.finish(attempt, status=response.status_code, error=error, receipt=receipt)
                return response.status_code
        except httpx.HTTPError:
            # Do not put tokens, URLs, response bodies or provider errors in the journal.
            self.journal.finish(attempt, error="transport_error")
            return None
