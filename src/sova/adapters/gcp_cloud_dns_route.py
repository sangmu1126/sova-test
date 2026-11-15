"""Route store on Google Cloud DNS: the DNS name clients use points at one side.

The Cloud DNS counterpart of ``Route53Route``, with the same contract:

* ``set`` swaps the record atomically: one change deletes the record exactly
  as it was read and adds the new one; Cloud DNS applies a change only if
  every deletion matches the current record, so an edit by someone else in
  between makes the change fail instead of being overwritten. It then waits,
  bounded, until Cloud DNS reports the change ``done``;
* ``get`` returns a side only if that change is done, the configured record
  is exactly that side's address and every authoritative name server of the
  zone answers exactly that address. Cloud DNS has no answer-testing API, so
  the servers are asked over DNS itself (``dns_query``), without recursion.

Clients that resolved the name before may keep the old address for up to the
record's TTL; ``ttl`` is reported and the source stays fenced meanwhile.
"""

import re
import time

from sova.adapters.dns_query import query_a
from sova.adapters.gcp_rest import GcpApiError, GcpRest
from sova.domain.contracts import ContractError

API = "https://dns.googleapis.com/dns/v1"
_ZONE = re.compile(r"[a-z][a-z0-9-]{0,62}")
_NAME = re.compile(r"(?=.{1,253}\.?$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?")
_IPV4 = re.compile(r"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)")


class CloudDnsRoute:
    def __init__(self, rest, *, zone, name, sides, ttl=60, sync_wait_seconds=120.0,
                 resolver=query_a):
        """``sides`` maps a route value (what the data adapter writes) to an IPv4."""
        if not isinstance(rest, GcpRest):
            raise ContractError("GCP REST access required")
        if type(zone) is not str or not _ZONE.fullmatch(zone):
            raise ContractError("Managed zone name required")
        if type(name) is not str or not _NAME.fullmatch(name.lower()):
            raise ContractError("DNS name required")
        if type(sides) is not dict or len(sides) < 2 or len(set(sides.values())) != len(sides):
            raise ContractError("A distinct address for each side required")
        for address in sides.values():
            if type(address) is not str or not _IPV4.fullmatch(address):
                raise ContractError("IPv4 address required")
        if type(ttl) is not int or not 1 <= ttl <= 86400:
            raise ContractError("Bounded TTL required")
        if not callable(resolver):
            raise ContractError("DNS resolver required")
        self.rest, self.zone, self.ttl, self.resolver = rest, zone, ttl, resolver
        self.name = name.lower().rstrip(".") + "."
        self.sides, self.sync_wait_seconds = dict(sides), sync_wait_seconds
        self.last_change = None
        self._servers = None

    def _url(self, path=""):
        return f"{API}/projects/{self.rest.project}/managedZones/{self.zone}{path}"

    def servers(self):
        if self._servers is None:
            zone = self.rest.get(self._url())
            if not self.name.endswith("." + zone["dnsName"]) and self.name != zone["dnsName"]:
                raise ContractError("The name is not in this zone")
            self._servers = tuple(sorted(zone.get("nameServers", ())))
            if not self._servers:
                raise ContractError("The zone has no name servers")
        return self._servers

    def _record(self):
        return self.rest.get(self._url(f"/rrsets/{self.name}/A"), missing_ok=True)

    def _change_done(self):
        if self.last_change is None:
            return True
        return self.rest.get(self._url(f"/changes/{self.last_change}"))["status"] == "done"

    def _answers(self):
        answers = {}
        for server in self.servers():
            try:
                code, authoritative, addresses = self.resolver(server, self.name)
            except OSError:
                answers[server] = {"error": "unreachable"}
                continue
            answers[server] = {"code": code, "authoritative": authoritative,
                               "addresses": addresses}
        return answers

    def observe(self):
        done = self._change_done()
        record = self._record()
        configured = sorted((record or {}).get("rrdatas", ()))
        answers = self._answers()
        agreed = {tuple(a.get("addresses", ())) for a in answers.values()
                  if a.get("code") == "NOERROR" and a.get("authoritative")}
        complete = len(agreed) == 1 and all(a.get("code") == "NOERROR"
                                            and a.get("authoritative") for a in answers.values())
        answer = sorted(next(iter(agreed))) if complete else None
        side = next((ref for ref, address in self.sides.items()
                     if answer == [address] and configured == [address]), None)
        return {"route": side if done else None, "answer": answer, "configured": configured,
                "servers": answers, "change_done": done,
                "ttl": record.get("ttl") if record else None}

    def get(self):
        return self.observe()["route"]

    def set(self, ref):
        if ref not in self.sides:
            raise ContractError("Unknown route side")
        current = self._record()
        wanted = {"name": self.name, "type": "A", "ttl": self.ttl,
                  "rrdatas": [self.sides[ref]]}
        if current is not None and current.get("rrdatas") == wanted["rrdatas"] \
                and current.get("ttl") == self.ttl:
            return  # already pointing there
        change = {"additions": [wanted]}
        if current is not None:
            change["deletions"] = [{k: current[k] for k in ("name", "type", "ttl", "rrdatas")}]
        try:
            made = self.rest.post(self._url("/changes"), change)
        except GcpApiError as error:
            if error.status in (409, 412) or error.reason in ("FAILED_PRECONDITION",
                                                              "ALREADY_EXISTS"):
                raise ContractError("The record changed in between; route not switched") \
                    from None
            raise
        self.last_change = made["id"]
        deadline = time.monotonic() + self.sync_wait_seconds
        while time.monotonic() < deadline and not self._change_done():
            time.sleep(2)
