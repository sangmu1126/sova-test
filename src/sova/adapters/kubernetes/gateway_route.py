"""Route store on a Gateway API ``HTTPRoute``: one rule sends its traffic to one side.

The successor of Ingress (ingress-nginx is retired) and what most managed and
on-premises clusters offer as their entry point. ``GatewayHttpRoute`` keeps
one rule of one HTTPRoute pointing all of its traffic at the Service of one
side, and answers ``get`` from three things that must agree:

* configuration: the rule has exactly one backend with weight, and it is a
  registered side's Service and port;
* acceptance: every registered parent Gateway reports the route ``Accepted``
  and its references ``ResolvedRefs`` for the route's current generation, so
  the implementation has taken this very version;
* traffic: every one of ``samples`` requests through the Gateway was answered
  by that side (``observe`` is the caller's probe of who answered: a marker
  the application serves, never a guess).

``set`` refuses a rule that is anything but a plain backend of a registered
side (someone else's split or filter is never overwritten by a switch), and
changes it with a JSON Patch that first tests the route's ``resourceVersion``.
It then waits, bounded, until the readback agrees. Connections already open
through the Gateway keep their upstream until they close; the source stays
fenced meanwhile, so a late request through one of them is refused.
"""

import http.client
import json
import ssl
import time
from urllib.parse import urlsplit

from sova.adapters.kubernetes.handoff_controls import KubernetesHandoffControls, _name
from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.domain.contracts import ContractError

_API = "/apis/gateway.networking.k8s.io/v1"


class GatewayHttpRoute:
    def __init__(self, controls, route, *, rule=0, parents, sides, observe, samples=6,
                 converge_seconds=60.0):
        """``parents``: the ``(namespace, name)`` of each Gateway the route must be
        accepted by; ``sides`` maps a route value to its Service ``(name, port)``;
        ``observe()`` returns the side marker of one request through the Gateway."""
        if not isinstance(controls, KubernetesHandoffControls):
            raise ContractError("Registered Kubernetes controls required")
        if type(rule) is not int or not 0 <= rule < 16:
            raise ContractError("A rule index required")
        if type(parents) is not tuple or not parents or not all(
                type(p) is tuple and len(p) == 2 for p in parents):
            raise ContractError("The parent Gateways as (namespace, name) required")
        if type(sides) is not dict or len(sides) < 2 or not all(
                type(v) is tuple and len(v) == 2 and type(v[1]) is int and 1 <= v[1] <= 65535
                for v in sides.values()) or len(set(sides.values())) != len(sides):
            raise ContractError("A distinct Service (name, port) for each side required")
        if not callable(observe) or type(samples) is not int or not 1 <= samples <= 50:
            raise ContractError("An observation probe and a bounded sample count required")
        self.controls, self.name, self.rule = controls, _name(route), rule
        self.parents = tuple((_name(ns), _name(n)) for ns, n in parents)
        self.sides = {ref: (_name(svc), port) for ref, (svc, port) in sides.items()}
        self.observe_one, self.samples = observe, samples
        self.converge_seconds = converge_seconds

    def _path(self):
        return f"{_API}/namespaces/{self.controls.registration.namespace}/httproutes/{self.name}"

    def _route(self):
        try:
            return self.controls._get(self._path())
        except KubernetesRequestRefused as refused:
            if refused.status == 404:
                raise ContractError("Registered HTTPRoute not found") from refused
            raise

    def _rule(self, route):
        rules = (route.get("spec") or {}).get("rules") or []
        if self.rule >= len(rules):
            raise ContractError("Registered rule not in the HTTPRoute")
        return rules[self.rule]

    def _configured(self, rule):
        """The side the rule sends everything to, or None."""
        weighted = [b for b in rule.get("backendRefs") or () if b.get("weight", 1) != 0]
        if len(weighted) != 1:
            return None
        backend = weighted[0]
        if backend.get("group", "") not in ("", "core") or \
                backend.get("kind", "Service") != "Service" or backend.get("namespace") not in \
                (None, self.controls.registration.namespace) or rule.get("filters"):
            return None
        wanted = (backend.get("name"), backend.get("port"))
        return next((ref for ref, svc in self.sides.items() if svc == wanted), None)

    def _accepted(self, route):
        generation = route["metadata"].get("generation")
        found = {}
        for parent in (route.get("status") or {}).get("parents") or ():
            ref = parent.get("parentRef") or {}
            key = (ref.get("namespace") or route["metadata"]["namespace"], ref.get("name"))
            conditions = {c.get("type"): c for c in parent.get("conditions") or ()}
            found[key] = all(
                (conditions.get(kind) or {}).get("status") == "True"
                and (conditions.get(kind) or {}).get("observedGeneration") == generation
                for kind in ("Accepted", "ResolvedRefs"))
        return {f"{ns}/{n}": found.get((ns, n), False) for ns, n in self.parents}

    def observe(self):
        route = self._route()
        configured = self._configured(self._rule(route))
        accepted = self._accepted(route)
        answers = []
        for _ in range(self.samples):
            try:
                answers.append(self.observe_one())
            except Exception as error:  # noqa: BLE001 - an unanswered probe decides nothing
                answers.append(f"error:{type(error).__name__}")
        served = set(answers)
        decided = configured if (configured is not None and all(accepted.values())
                                 and served == {configured}) else None
        return {"configured": configured, "accepted": accepted, "answers": answers[:20],
                "route": decided}

    def get(self):
        return self.observe()["route"]

    def set(self, ref):
        if ref not in self.sides:
            raise ContractError("Unknown route side")
        route = self._route()
        rule = self._rule(route)
        current = self._configured(rule)
        if current is None and rule.get("backendRefs"):
            raise ContractError("The rule is not a plain backend of a registered side; "
                                "it is not overwritten")
        if current != ref:
            name, port = self.sides[ref]
            self._patch_backends(route, [{"name": name, "port": port}])
        # The implementation programs its proxies asynchronously: wait, bounded,
        # until configuration, acceptance and traffic agree.
        deadline = time.monotonic() + self.converge_seconds
        while self.get() != ref and time.monotonic() < deadline:
            time.sleep(0.5)


    # -- pausing: new requests reach no side, open connections keep flowing --------------
    def _patch_backends(self, route, backends):
        body = json.dumps([
            {"op": "test", "path": "/metadata/resourceVersion",
             "value": route["metadata"]["resourceVersion"]},
            {"op": "replace", "path": f"/spec/rules/{self.rule}/backendRefs",
             "value": backends}]).encode()
        self.controls._request("PATCH", self._path(), {}, timeout=5, body=body)

    def pause(self, ref=None):
        """Leave the rule without a backend: the Gateway answers new requests with
        an error and sends nothing on; streams already open keep their upstream."""
        route = self._route()
        rule = self._rule(route)
        if not rule.get("backendRefs"):
            return
        if self._configured(rule) is None:
            raise ContractError("The rule is not a plain backend of a registered side; "
                                "it is not overwritten")
        self._patch_backends(route, [])

    def paused(self, ref=None):
        """(paused, observation): no backend configured, the Gateway has accepted
        that, and no sample request was answered by a side."""
        route = self._route()
        empty = not self._rule(route).get("backendRefs")
        seen = self.observe()
        answered = [a for a in seen["answers"] if a in self.sides]
        return empty and all(seen["accepted"].values()) and not answered, {
            **seen, "backends": "none" if empty else "configured"}

    def left(self, ref):
        return self._configured(self._rule(self._route())) != ref


def http_side_observer(url, *, header, sides, ca_file=None, timeout=5.0):
    """``observe()`` for a route store: one GET of ``url`` (HTTPS, the server's
    certificate verified; plain HTTP only on loopback), answered by the side
    whose registered value the response header ``header`` carries."""
    parts = urlsplit(url)
    loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme not in ("https", "http") or (parts.scheme == "http" and not loopback) \
            or not parts.hostname or parts.username is not None or parts.password is not None \
            or parts.fragment:
        raise ContractError("An HTTPS probe URL required")
    if type(header) is not str or not header or type(sides) is not dict or not sides:
        raise ContractError("A marker header and the value of each side required")
    if (any(type(value) is not str or not value or len(value) > 128 for value in sides.values())
            or len(set(sides.values())) != len(sides)):
        raise ContractError("Distinct bounded side marker values required")
    context = ssl.create_default_context(cafile=ca_file) if parts.scheme == "https" else None
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

    def observe():
        if context is not None:
            connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443,
                                                     timeout=timeout, context=context)
        else:
            connection = http.client.HTTPConnection(parts.hostname, parts.port or 80,
                                                    timeout=timeout)
        try:  # a new connection each time: a kept-alive one would keep its old upstream
            connection.request("GET", path, headers={"Cache-Control": "no-cache",
                                                     "Connection": "close"})
            response = connection.getresponse()
            response.read(65536)
            value = response.getheader(header)
            if not 200 <= response.status < 300:
                return f"http-status:{response.status}"
        finally:
            connection.close()
        return next((ref for ref, marker in sides.items() if marker == value),
                    f"unmarked:{value}"[:80])
    return observe
