"""Route store on Amazon Route 53: the DNS name clients use points at one side.

A data adapter's ``commit_route`` writes to a route store (``get``/``set``).
``Route53Route`` keeps one record in a hosted zone pointing at one side's
address and answers ``get`` from what Route 53 answers for the name now:

* ``set`` swaps the record atomically: one change batch deletes the record
  exactly as it was read and creates the new one, so a change made by someone
  else in between makes the whole batch fail instead of being overwritten;
  it then waits, bounded, until Route 53 reports the change in sync;
* ``get`` returns a side only if the latest change is in sync and both the
  configured record and Route 53's own answer for the name (``TestDNSAnswer``)
  are exactly that side's address; while any change, including someone
  else's, is still propagating the two differ and no side is claimed.

Clients that resolved the name before may keep the old address for up to the
record's TTL. That window is not hidden: ``ttl`` is reported, and a handoff
covers it by keeping the source fenced, so a stale client is refused there.
"""

import re
import time

from sova.domain.contracts import ContractError

_ZONE = re.compile(r"[A-Z0-9]{1,32}")
_NAME = re.compile(r"(?=.{1,253}\.?$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?")
_IPV4 = re.compile(r"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)")


class Route53Route:
    def __init__(self, route53, *, zone_id, name, sides, ttl=60, sync_wait_seconds=120.0):
        """``sides`` maps a route value (what the data adapter writes) to an IPv4."""
        if not callable(getattr(route53, "change_resource_record_sets", None)):
            raise ContractError("Route 53 client required")
        if type(zone_id) is not str or not _ZONE.fullmatch(zone_id):
            raise ContractError("Hosted zone ID required")
        if type(name) is not str or not _NAME.fullmatch(name.lower()):
            raise ContractError("DNS name required")
        if type(sides) is not dict or len(sides) < 2 or len(set(sides.values())) != len(sides):
            raise ContractError("A distinct address for each side required")
        for address in sides.values():
            if type(address) is not str or not _IPV4.fullmatch(address):
                raise ContractError("IPv4 address required")
        if type(ttl) is not int or not 1 <= ttl <= 86400:
            raise ContractError("Bounded TTL required")
        self.r53, self.zone, self.ttl = route53, zone_id, ttl
        self.name = name.lower().rstrip(".") + "."
        self.sides, self.sync_wait_seconds = dict(sides), sync_wait_seconds
        self.last_change = None

    def _record(self):
        found = self.r53.list_resource_record_sets(
            HostedZoneId=self.zone, StartRecordName=self.name, StartRecordType="A",
            MaxItems="1")["ResourceRecordSets"]
        if found and found[0]["Name"] == self.name and found[0]["Type"] == "A":
            return found[0]
        return None

    def _answer(self):
        answer = self.r53.test_dns_answer(HostedZoneId=self.zone, RecordName=self.name,
                                          RecordType="A")
        return answer.get("ResponseCode"), sorted(answer.get("RecordData", ()))

    def observe(self):
        in_sync = self.last_change is None or self.r53.get_change(
            Id=self.last_change)["ChangeInfo"]["Status"] == "INSYNC"
        code, data = self._answer()
        record = self._record()
        configured = sorted(r["Value"] for r in (record or {}).get("ResourceRecords", ()))
        # A side only when what is configured and what is answered agree on it:
        # right after any change, someone else's included, they can differ.
        side = next((ref for ref, address in self.sides.items()
                     if data == [address] and configured == [address]), None)
        return {"route": side if in_sync and code == "NOERROR" else None,
                "answer": data, "configured": configured, "response_code": code,
                "change_in_sync": in_sync, "ttl": record["TTL"] if record else None}

    def get(self):
        return self.observe()["route"]

    def set(self, ref):
        if ref not in self.sides:
            raise ContractError("Unknown route side")
        current = self._record()
        wanted = {"Name": self.name, "Type": "A", "TTL": self.ttl,
                  "ResourceRecords": [{"Value": self.sides[ref]}]}
        if current is not None and current.get("ResourceRecords") == wanted["ResourceRecords"] \
                and current.get("TTL") == self.ttl:
            return  # already pointing there
        changes = ([{"Action": "DELETE", "ResourceRecordSet": current}] if current else [])
        changes.append({"Action": "CREATE", "ResourceRecordSet": wanted})
        try:
            change = self.r53.change_resource_record_sets(
                HostedZoneId=self.zone, ChangeBatch={
                    "Comment": "sova handoff commit_route", "Changes": changes})
        except self.r53.exceptions.InvalidChangeBatch as error:
            raise ContractError("The record changed in between; route not switched") from error
        self.last_change = change["ChangeInfo"]["Id"]
        deadline = time.monotonic() + self.sync_wait_seconds
        while time.monotonic() < deadline and self.r53.get_change(
                Id=self.last_change)["ChangeInfo"]["Status"] != "INSYNC":
            time.sleep(3)
