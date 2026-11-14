"""Route 53 route decisions against a stub (no AWS)."""

import pytest

from sova.adapters.aws_route53_route import Route53Route
from sova.domain.contracts import ContractError

SIDES = {"src": "198.51.100.10", "tgt": "203.0.113.20"}


class R53:
    class exceptions:
        class InvalidChangeBatch(Exception):
            pass

    def __init__(self, configured, answered):
        self.configured, self.answered, self.batches = configured, answered, []

    def list_resource_record_sets(self, **_):
        if self.configured is None:
            return {"ResourceRecordSets": []}
        return {"ResourceRecordSets": [{"Name": "orders.example.test.", "Type": "A",
                                        "TTL": 60, "ResourceRecords": [
                                            {"Value": self.configured}]}]}

    def test_dns_answer(self, **_):
        return {"ResponseCode": "NOERROR", "RecordData": [self.answered]}

    def change_resource_record_sets(self, *, HostedZoneId, ChangeBatch):
        self.batches.append(ChangeBatch["Changes"])
        deleted = [c for c in ChangeBatch["Changes"] if c["Action"] == "DELETE"]
        if deleted and deleted[0]["ResourceRecordSet"]["ResourceRecords"][0]["Value"] != \
                self.configured:
            raise self.exceptions.InvalidChangeBatch("record changed")
        self.configured = self.answered = ChangeBatch["Changes"][-1]["ResourceRecordSet"][
            "ResourceRecords"][0]["Value"]
        return {"ChangeInfo": {"Id": "C1"}}

    def get_change(self, Id):
        return {"ChangeInfo": {"Status": "INSYNC"}}


def route(r53):
    return Route53Route(r53, zone_id="Z0123456789", name="orders.example.test", sides=SIDES)


def test_a_switch_deletes_exactly_what_was_read_and_creates_the_new_record():
    r53 = R53(SIDES["src"], SIDES["src"])
    r = route(r53)
    assert r.get() == "src"
    r.set("tgt")
    actions = [(c["Action"], c["ResourceRecordSet"]["ResourceRecords"][0]["Value"])
               for c in r53.batches[0]]
    assert actions == [("DELETE", SIDES["src"]), ("CREATE", SIDES["tgt"])]
    assert r.get() == "tgt"
    r.set("tgt")  # already there: no second change
    assert len(r53.batches) == 1


def test_configured_and_answered_must_agree():
    assert route(R53(SIDES["tgt"], SIDES["src"])).get() is None  # still propagating
    assert route(R53("192.0.2.99", "192.0.2.99")).get() is None  # neither side


def test_a_record_changed_in_between_is_refused():
    r53 = R53(SIDES["src"], SIDES["src"])
    r = route(r53)
    stale = r._record()
    r53.configured = "192.0.2.99"
    r._record = lambda: stale
    with pytest.raises(ContractError, match="changed in between"):
        r.set("tgt")


@pytest.mark.parametrize("kwargs", [{"zone_id": "z!"}, {"name": "not a name"},
                                    {"sides": {"a": "1.1.1.1"}},
                                    {"sides": {"a": "1.1.1.1", "b": "1.1.1.1"}},
                                    {"sides": {"a": "1.1.1.1", "b": "::1"}}, {"ttl": 0}])
def test_unbounded_or_ambiguous_routes_are_refused(kwargs):
    args = {"zone_id": "Z0123456789", "name": "orders.example.test", "sides": SIDES, **kwargs}
    with pytest.raises(ContractError):
        Route53Route(R53(None, None), **args)
