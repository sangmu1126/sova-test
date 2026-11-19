"""Synthetic projection fixtures; these do not claim live discovery coverage."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from measured_dependency_snapshot import snapshot_from_facts
from sova.application.service_contracts import example_service_contract
from sova.domain.contracts import ContractError


def fixture():
    now=datetime.now(UTC)
    contract=example_service_contract()
    digest="sha256:"+"a"*64
    facts={"observed_at":now.isoformat(),"run_id":"b0be270e62",
        "aws":{"account":"265233844540","instance_id":"i-04946f0e0c0548c3b"},
        "target":{"namespace_uid":"565b91c4-4f6e-4d06-94c6-9f8803afa6c9"},"business_data_changed":False,
        "source":{"application_projection":{"tenant":contract.tenant_id,"application":contract.service_id,
            "run":"b0be270e62","database":"source_orders","role":"orders_app","image_digest":digest,
            "expires_at":(now+timedelta(minutes=30)).isoformat()},
            "containers":[{"name":name,"run_id":"b0be270e62","running":True,"id":"synthetic-"+name,
                "registered_image":"local.invalid/image@"+digest} for name in ("orders-app","orders-db")]},
        "databases":{"source":{"system_identifier":"7709720208587968539","database":"source_orders",
            "database_oid":"12345","tls":[True],"version":"17.12",
            "roles":[{"name":"orders_batch","oid":"54321"}],
            "tables":[[name,str(i),"postgres"] for i,name in enumerate(("orders","work_items","idempotency_results"))]}}}
    return now,contract,facts


def test_actual_identifier_projection_keeps_unproven_coverage_incomplete():
    now,contract,facts=fixture()
    facts["writer_inventory_complete"]=facts["control_inventory_complete"]=True
    snapshot=snapshot_from_facts(facts,contract,now=now)
    assert len(snapshot.graph.resources)==3 and len(snapshot.graph.relations)==2
    assert dict(snapshot.resource_uids)["source-api"]=="synthetic-orders-app"
    assert snapshot.writer_inventory_complete is snapshot.control_inventory_complete is False
    assert len(snapshot.unresolved_paths)==5
    assert dict(snapshot.dataset_nodes)=={name:"source-database" for name in ("orders","work_items","idempotency_results")}


@pytest.mark.parametrize("age",(-1,600,601))
def test_future_and_stale_facts_are_refused(age):
    now,contract,facts=fixture()
    facts["observed_at"]=(now-timedelta(seconds=age)).isoformat()
    with pytest.raises(ContractError):
        snapshot_from_facts(facts,contract,now=now)


@pytest.mark.parametrize("target",("account","namespace","database","artifact","tenant","running"))
def test_wrong_resource_scope_or_artifact_is_not_an_observed_graph(target):
    now,contract,facts=fixture()
    facts=deepcopy(facts)
    if target=="account":
        facts["aws"]["account"]="other"
    if target=="namespace":
        facts["target"]["namespace_uid"]="other"
    if target=="database":
        facts["databases"]["source"]["system_identifier"]="other"
    if target=="artifact":
        facts["source"]["application_projection"]["image_digest"]="sha256:"+"b"*64
    if target=="tenant":
        facts["source"]["application_projection"]["tenant"]="other"
    if target=="running":
        facts["source"]["containers"][0]["running"]=False
    with pytest.raises(ContractError):
        snapshot_from_facts(facts,contract,now=now)
