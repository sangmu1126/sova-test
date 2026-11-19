"""Bounded Cost Explorer observations for an operator-registered RunId.

A missing/inactive allocation tag or empty billing response is UNKNOWN, not
zero. This is an attributed historical observation, never a complete TCO
estimate, spending cap, savings recommendation or migration authorization.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from sova.domain.contracts import ContractError, identifier


class AWSRunCostObservation:
    def __init__(self, client, *, run_id, account_id):
        self.client=client
        self.run_id=identifier(run_id)
        if not isinstance(account_id,str) or len(account_id)!=12 or not account_id.isdecimal():
            raise ContractError("Registered twelve-digit account required")
        self.account_id=account_id

    def observe(self, *, start, end):
        if type(start) is not date or type(end) is not date or not start<end or (end-start).days>366:
            raise ContractError("Bounded historical date interval required")
        base={"kind":"attributed_billing_observation","account_id":self.account_id,"run_id":self.run_id,
            "period":{"start":start.isoformat(),"end_exclusive":end.isoformat()},"amount":None,
            "currency":None,"complete_tco":False,"comparison_authorized":False,"execution_authorized":False}
        tags=[]
        token=None
        seen=set()
        for _ in range(4):
            args={"TagKeys":["RunId"],"MaxResults":100}
            if token:
                args["NextToken"]=token
            response=self.client.list_cost_allocation_tags(**args)
            tags += response.get("CostAllocationTags",[])
            token=response.get("NextToken")
            if not token:
                break
            if token in seen:
                raise ContractError("Repeated billing tag pagination")
            seen.add(token)
        else:
            raise ContractError("Billing tag inventory exceeds its bound")
        exact=[t for t in tags if t.get("TagKey")=="RunId"]
        if len(exact)!=1 or exact[0].get("Status")!="Active":
            return {**base,"available":False,"reason":"allocation_tag_not_active","estimated":None}
        totals=[]
        token=None
        seen=set()
        for _ in range(4):
            args={"TimePeriod":{"Start":start.isoformat(),"End":end.isoformat()},"Granularity":"DAILY",
                "Metrics":["UnblendedCost"],"Filter":{"And":[{"Dimensions":{"Key":"LINKED_ACCOUNT","Values":[self.account_id]}},
                    {"Tags":{"Key":"RunId","Values":[self.run_id],"MatchOptions":["EQUALS"]}}]}}
            if token:
                args["NextPageToken"]=token
            response=self.client.get_cost_and_usage(**args)
            totals+=response.get("ResultsByTime",[])
            token=response.get("NextPageToken")
            if not token:
                break
            if token in seen:
                raise ContractError("Repeated cost pagination")
            seen.add(token)
        else:
            raise ContractError("Billing observation exceeds its bound")
        if not totals:
            return {**base,"available":False,"reason":"billing_data_unavailable","estimated":None}
        amount=Decimal(0)
        periods=set()
        for row in totals:
            period=row.get("TimePeriod",{})
            try:
                left,right=(date.fromisoformat(period[k]) for k in ("Start","End"))
                value=row["Total"]["UnblendedCost"]
                current=Decimal(value["Amount"])
            except (KeyError,TypeError,ValueError,InvalidOperation) as exc:
                raise ContractError("Invalid billing observation") from exc
            if (not start<=left<right<=end or (right-left).days!=1 or (left,right) in periods or value["Unit"]!="USD"
                    or not current.is_finite() or abs(current)>Decimal("1e18") or type(row.get("Estimated")) is not bool):
                raise ContractError("Ambiguous, duplicate or out-of-scope cost observation")
            periods.add((left,right))
            amount+=current
        if len(periods)!=(end-start).days:
            return {**base,"available":False,"reason":"billing_interval_incomplete","estimated":None,
                "observed_partial_amount":str(amount),"observed_partial_currency":"USD"}
        if amount == 0 and any(r["Estimated"] for r in totals):
            # A newly activated tag can return a synthetic estimated zero
            # before AWS has attributed any usage. Do not call the run free.
            return {**base,"available":False,"reason":"estimated_zero_not_verified",
                "estimated":True,"provider_reported_amount":str(amount),
                "provider_reported_currency":"USD","daily_intervals":len(periods)}
        return {**base,"available":True,"reason":None,"amount":str(amount),"currency":"USD",
            "estimated":any(r["Estimated"] for r in totals),"daily_intervals":len(periods),
            "unattributed_cost_is_not_zero":True,"coverage":"Costs carrying this active RunId only; excludes unallocated/shared/local-VM TCO"}
