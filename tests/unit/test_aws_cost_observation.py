"""AWS response doubles; no claim of live billing or complete TCO."""

from datetime import date
from unittest.mock import Mock

import pytest

from sova.adapters.aws_cost_observation import AWSRunCostObservation
from sova.domain.contracts import ContractError


def setup(rows):
    client=Mock()
    client.list_cost_allocation_tags.return_value={"CostAllocationTags":[{"TagKey":"RunId","Status":"Active"}]}
    client.get_cost_and_usage.return_value={"ResultsByTime":rows}
    return client,AWSRunCostObservation(client,run_id="registered-run",account_id="123456789012")


def row(start="2026-11-18",end="2026-11-19",amount="0.12",**overrides):
    return {"TimePeriod":{"Start":start,"End":end},"Total":{"UnblendedCost":{"Amount":amount,"Unit":"USD"}},
        "Estimated":True,**overrides}


def test_inactive_allocation_does_not_query_cost_or_report_zero():
    client,observer=setup([])
    client.list_cost_allocation_tags.return_value={"CostAllocationTags":[]}
    result=observer.observe(start=date(2026,11,18),end=date(2026,11,19))
    assert not result["available"] and result["amount"] is None
    client.get_cost_and_usage.assert_not_called()


def test_real_response_shape_is_exactly_scoped_and_still_not_tco_or_authority():
    client,observer=setup([row()])
    result=observer.observe(start=date(2026,11,18),end=date(2026,11,19))
    assert result["amount"]=="0.12" and result["estimated"] is True
    assert result["complete_tco"] is result["comparison_authorized"] is result["execution_authorized"] is False
    query=client.get_cost_and_usage.call_args.kwargs
    assert query["Filter"]=={"And":[{"Dimensions":{"Key":"LINKED_ACCOUNT","Values":["123456789012"]}},
        {"Tags":{"Key":"RunId","Values":["registered-run"],"MatchOptions":["EQUALS"]}}]}


@pytest.mark.parametrize("rows",([], [row()]))
def test_empty_or_partial_interval_does_not_report_a_complete_amount(rows):
    _,observer=setup(rows)
    result=observer.observe(start=date(2026,11,18),end=date(2026,11,20))
    assert not result["available"] and result["amount"] is None


@pytest.mark.parametrize("rows",([row(),row()], [row(amount="NaN")], [row(amount="Infinity")],
    [row(end="2026-11-20")], [row(Estimated="true")], [row(start="2026-11-17",end="2026-11-18")]))
def test_malformed_duplicate_nonfinite_and_outside_scope_billing_is_rejected(rows):
    _,observer=setup(rows)
    with pytest.raises(ContractError):
        observer.observe(start=date(2026,11,18),end=date(2026,11,19))


def test_billing_pagination_cannot_loop_or_duplicate_amounts():
    client,observer=setup([])
    client.get_cost_and_usage.return_value={"ResultsByTime":[row()],"NextPageToken":"repeated"}
    with pytest.raises(ContractError,match="Repeated"):
        observer.observe(start=date(2026,11,18),end=date(2026,11,19))


def test_estimated_zero_is_unknown_until_attribution_is_verified():
    _,observer=setup([row(amount='0')])
    result=observer.observe(start=date(2026,11,18),end=date(2026,11,19))
    assert result['amount'] is None and result['available'] is False
    assert result['reason']=='estimated_zero_not_verified'
    assert result['provider_reported_amount']=='0' and result['estimated'] is True


def test_final_zero_is_preserved_as_a_scoped_final_billing_result():
    _,observer=setup([row(amount='0',Estimated=False)])
    result=observer.observe(start=date(2026,11,18),end=date(2026,11,19))
    assert result['amount']=='0' and result['available'] is True
    assert result['estimated'] is False and result['complete_tco'] is False
