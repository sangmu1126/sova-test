"""Execution of PostgreSQL handoffs for the worker: the adapter of the plan's
manifest and the measurement of every obligation kind it can verify.

Where an application's databases are and which tables move is deployment
configuration, resolved per (tenant, application); the measurement maps each
obligation kind to an independent probe (``PostgresHandoffProbes``), never to
the adapter's own readback. A residency obligation carries its allowed zones
in the compiled plan, so nothing here reads the contract again.
"""

from sova.adapters.postgres_handoff_probes import PostgresHandoffProbes
from sova.adapters.postgres_logical_replication import PostgresLogicalReplicationAdapter
from sova.adapters.postgres_offline_copy import PostgresOfflineCopyAdapter
from sova.domain.contracts import ContractError
from sova.service.worker import Execution


def postgres_measure(probes, *, other_db, scratch_db):
    def measure(obligation, times):
        kind, oid = obligation["kind"], obligation["id"]
        if kind == "placement_evidence":
            return probes.zone("target", set(obligation["allowed_zones"]))
        if kind == "return_target_check":
            return probes.write_denied("target")
        if oid == "fence.source_writes_denied" or kind == "stale_route_probe":
            return probes.write_denied("source")
        if kind == "final_content_comparison":
            return probes.content_equal(oid.split(".", 1)[1])
        if kind == "business_invariant":
            return probes.order_effects_once()
        if oid == "security.deny_cross_tenant_data_access":
            return probes.cross_tenant_denied(other_db)
        if oid == "security.verified_transport_encryption":
            return probes.transport_encrypted("target")
        if kind == "restore_drill" or oid == "security.tested_restore":
            return probes.restore_drill(scratch_db)
        if kind == "authority_readback":
            return probes.single_writer()
        if kind == "measured_budget":
            end = ("activate_target" if oid.endswith("write_admission_pause")
                   else "commit_route")
            if "pause_admission" not in times or end not in times:
                return False, {"probe": "measured_budget", "reason": "step not observed"}
            spent = (times[end] - times["pause_admission"]).total_seconds() * 1000
            return spent <= obligation["budget_ms"], {
                "probe": "measured_budget", "spent_ms": round(spent, 3),
                "budget_ms": obligation["budget_ms"]}
        return False, {"probe": "none", "reason": f"no probe for {kind}"}
    return measure


def postgres_factory(adapter_id, resolve, *, cdc=False):
    """A catalog factory: ``resolve(tenant, application)`` gives the
    application's databases (connect, connect_as, names, roles, tables,
    schema, probe insert, route, and for CDC the source as seen by the target)."""

    def factory(tenant_id, application_id, revision):
        d = resolve(tenant_id, application_id)
        common = dict(connect=d["connect"], source_db=d["source_db"],
                      target_db=d["target_db"], app_role=d["app_role"], tables=d["tables"],
                      schema_sql=d["schema_sql"], route=d["route"],
                      writer_roles=d["writer_roles"])
        if cdc:
            if not d.get("source_conninfo"):
                raise ContractError("CDC needs the source as the target server sees it")
            adapter = PostgresLogicalReplicationAdapter(adapter_id,
                                                        source_conninfo=d["source_conninfo"],
                                                        **common)
        else:
            adapter = PostgresOfflineCopyAdapter(adapter_id, **common)
        probes = PostgresHandoffProbes(
            connect_as=d["connect_as"], admin_connect=d["connect"], source_db=d["source_db"],
            target_db=d["target_db"], app_role=d["app_role"], tables=d["tables"],
            probe_insert=d["probe_insert"], writer_roles=d["writer_roles"])
        return Execution(adapter, postgres_measure(probes, other_db=d["other_db"],
                                                   scratch_db=d["scratch_db"]))

    return factory
