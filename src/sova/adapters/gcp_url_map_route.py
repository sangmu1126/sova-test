"""Route store on a Google Cloud Application Load Balancer: the URL map's default
backend is one side.

The GCP counterpart of ``AlbListenerRoute``:

* ``set`` reads the URL map and refuses if it routes anything by host or path
  rules (a switch never overwrites rules it does not own); it writes the new
  default service with the map's fingerprint, so an edit by someone else in
  between makes the write fail instead of being lost, and waits for the
  global operation;
* ``get`` returns a side only if the map sends everything to that side's
  backend service and every one of ``samples`` requests through the load
  balancer was answered by that side. URL map changes reach Google's front
  ends over minutes; until then the answers disagree and no side is claimed.
"""

import re
import time

from sova.adapters.gcp_rest import GcpApiError, GcpRest
from sova.domain.contracts import ContractError

API = "https://compute.googleapis.com/compute/v1"
_NAME = re.compile(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?")


def _tail(link):
    return (link or "").rsplit("/", 1)[-1]


class UrlMapRoute:
    def __init__(self, rest, *, url_map, sides, observe, samples=6, sync_wait_seconds=900.0):
        """``sides`` maps a route value to a backend service name; ``observe()``
        returns the side marker of one request through the load balancer."""
        if not isinstance(rest, GcpRest):
            raise ContractError("GCP REST access required")
        if type(url_map) is not str or not _NAME.fullmatch(url_map):
            raise ContractError("URL map name required")
        if type(sides) is not dict or len(sides) < 2 or len(set(sides.values())) != len(sides):
            raise ContractError("A distinct backend service for each side required")
        for name in sides.values():
            if type(name) is not str or not _NAME.fullmatch(name):
                raise ContractError("Backend service name required")
        if not callable(observe) or type(samples) is not int or not 1 <= samples <= 50:
            raise ContractError("An observation probe and a bounded sample count required")
        self.rest, self.sides = rest, dict(sides)
        self.url = f"{API}/projects/{rest.project}/global/urlMaps/{url_map}"
        self.observe_one, self.samples = observe, samples
        self.sync_wait_seconds = sync_wait_seconds

    def _map(self):
        return self.rest.get(self.url)

    def _configured(self, found):
        if found.get("hostRules") or found.get("pathMatchers") or found.get("defaultRouteAction"):
            return None, True  # routes by rules this store does not own
        backend = _tail(found.get("defaultService"))
        return next((ref for ref, name in self.sides.items() if name == backend), None), False

    def observe(self):
        configured, _ = self._configured(self._map())
        answers = []
        for _ in range(self.samples):
            try:
                answers.append(self.observe_one())
            except OSError:
                answers.append(None)
        served = answers[0] if answers and len(set(answers)) == 1 else None
        return {"route": configured if served == configured else None,
                "configured": configured, "answers": answers}

    def get(self):
        return self.observe()["route"]

    def _wait(self, operation):
        url = operation.get("selfLink")
        deadline = time.monotonic() + 300
        while url and operation.get("status") != "DONE" and time.monotonic() < deadline:
            time.sleep(2)
            operation = self.rest.get(url)
        if operation.get("error"):
            raise ContractError("The URL map change failed")

    def set(self, ref):
        if ref not in self.sides:
            raise ContractError("Unknown route side")
        found = self._map()
        configured, ruled = self._configured(found)
        if ruled:
            raise ContractError("The URL map routes by rules; route not switched")
        if configured != ref:
            backend = (f"{API}/projects/{self.rest.project}/global/backendServices/"
                       f"{self.sides[ref]}")
            try:
                operation = self.rest.patch(self.url, {"defaultService": backend,
                                                       "fingerprint": found["fingerprint"]})
            except GcpApiError as error:
                if error.status in (409, 412) or error.reason in ("FAILED_PRECONDITION",
                                                                  "ABORTED"):
                    raise ContractError("The URL map changed in between; route not switched") \
                        from None
                raise
            self._wait(operation)
        deadline = time.monotonic() + self.sync_wait_seconds
        while time.monotonic() < deadline and self.get() != ref:
            time.sleep(5)
