"""Execution of MySQL handoffs for the worker: the MySQL reference adapter and
its independent probes, measured like any data store (``data_measure``).

``resolve(tenant, application)`` gives the application's databases:
``connect(database)`` (administrative, no database selected),
``connect_as(database, account)``, the database names, the writer accounts
(``user@host``), tables, schema, probe insert, route, and ``zone_of``; for
change capture also ``replication`` (``source_endpoint``, ``account``,
``password``, ``tls``).
"""

from sova.adapters.mysql_handoff_probes import MySqlHandoffProbes
from sova.adapters.mysql_offline_copy import MySqlOfflineCopyAdapter
from sova.adapters.mysql_replication import MySqlReplicationAdapter
from sova.domain.contracts import ContractError
from sova.service.postgres_execution import data_measure
from sova.service.worker import Execution


def mysql_factory(adapter_id, resolve, *, replication=False):
    """``replication``: the change-capture adapter (``d["replication"]`` gives the
    source as the target server reaches it and the replication account)."""
    def factory(tenant_id, application_id, revision):
        d = resolve(tenant_id, application_id)
        common = dict(connect=d["connect"], source_db=d["source_db"],
                      target_db=d["target_db"], app_account=d["app_role"], tables=d["tables"],
                      schema_sql=d["schema_sql"], route=d["route"],
                      writer_accounts=d["writer_roles"])
        if replication:
            r = d.get("replication")
            if not r:
                raise ContractError("Change capture needs the replication settings")
            adapter = MySqlReplicationAdapter(
                adapter_id, source_endpoint=r["source_endpoint"],
                replication_account=r["account"], replication_password=r["password"],
                replication_tls=r.get("tls", False), replication_ca_path=r.get("ca_path"),
                **common)
        else:
            adapter = MySqlOfflineCopyAdapter(adapter_id, **common)
        probes = MySqlHandoffProbes(
            connect_as=d["connect_as"], admin_connect=d["connect"], source_db=d["source_db"],
            target_db=d["target_db"], app_account=d["app_role"], tables=d["tables"],
            probe_insert=d["probe_insert"], writer_accounts=d["writer_roles"],
            zone_of=d.get("zone_of"))
        return Execution(adapter, data_measure(probes, other_db=d["other_db"],
                                               scratch_db=d["scratch_db"]))

    return factory
