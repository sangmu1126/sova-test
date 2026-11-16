"""Independent probes for a session server's handoff (§16.3, §18.2).

A session server has no content to compare; what its obligations ask is who
can still serve. Read from the cluster separately from the adapter:

* a side cannot serve when its workload wants no replica and no Pod of it is
  alive (a terminating Pod still serves the connections it holds);
* the target alone serves when it has a ready Pod, the route reaches no source
  Pod (at activation it is still paused; that it reaches the target is the
  route step's obligation), and the source cannot serve;
* placement is the inventory's ``zone_of(side)``, reported as read.

Each probe returns ``(passed, observation)``.
"""

from sova.adapters.kubernetes.volume_controls import alive, matches, ready


class KubernetesSessionProbes:
    def __init__(self, *, source, target, route, routes, zone_of=None):
        """``source``/``target`` are the adapter's ``SessionSide`` values; ``route``
        the Service route store and ``routes`` its (source, target, paused) values."""
        self.sides = {"source": source, "target": target}
        self.route, self.routes, self.zone_of = route, routes, zone_of

    def _pods(self, side):
        s = self.sides[side]
        found = s.controls.workload(*s.app)
        if found is None:
            return None, []
        selector = (found.get("spec") or {}).get("selector", {}).get("matchLabels") or {}
        pods = [p for p in s.controls.all_pods() if selector and matches(p, selector)
                and alive(p)]
        return found, pods

    def write_denied(self, side):
        found, pods = self._pods(side)
        wanted = None if found is None else (found.get("spec") or {}).get("replicas")
        denied = found is None or (wanted == 0 and not pods)
        return denied, {"probe": "write_denied", "side": side,
                        "server": "/".join(self.sides[side].app), "exists": found is not None,
                        "desired": wanted,
                        "pods_alive": sorted(p["metadata"]["name"] for p in pods)[:20]}

    def single_writer(self):
        _, pods = self._pods("target")
        serving = sorted(p["metadata"]["name"] for p in pods if ready(p))
        route = self.route.get()
        source_denied, source = self.write_denied("source")
        return bool(serving) and route != self.routes[0] and source_denied, {
            "probe": "single_writer", "target_serving": serving[:20], "route": route,
            "source": source}

    def zone(self, side, allowed):
        label = self.zone_of(side) if self.zone_of else None
        return label in allowed, {"probe": "zone", "side": side, "label": label,
                                  "allowed": sorted(allowed),
                                  "source": "inventory" if self.zone_of else "none"}


def session_measure(probes):
    """The obligation kinds a session server answers; content belongs to others."""
    def measure(obligation, times):
        kind, oid = obligation["kind"], obligation["id"]
        if kind == "placement_evidence":
            return probes.zone("target", set(obligation["allowed_zones"]))
        if kind == "return_target_check":
            return probes.write_denied("target")
        if oid == "fence.source_writes_denied" or kind == "stale_route_probe":
            return probes.write_denied("source")
        if kind == "authority_readback":
            return probes.single_writer()
        return False, {"probe": "none", "reason": f"a session server has no probe for {kind}"}
    return measure
