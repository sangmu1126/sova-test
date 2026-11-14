"""Construction rules of the logical-replication adapter (no database needed)."""

import pytest

from sova.adapters.postgres_logical_replication import (
    PostgresLogicalReplicationAdapter,
    replication_name,
)
from sova.domain.contracts import ContractError


class Route:
    def get(self):
        return "src"

    def set(self, value):
        pass


def adapter(**overrides):
    values = dict(connect=lambda db: None, source_db="src", target_db="tgt", app_role="app",
                  tables=(("orders", "id"),), schema_sql="SELECT 1", route=Route(),
                  source_conninfo="user=postgres dbname=src")
    return PostgresLogicalReplicationAdapter("pg-logical", **(values | overrides))


def test_one_name_per_direction_that_postgres_accepts():
    forward, reverse = replication_name("src", "tgt"), replication_name("tgt", "src")
    assert forward == replication_name("src", "tgt")  # a fresh instance finds the same objects
    assert forward != reverse  # a failback never adopts the forward slot
    assert forward.startswith("sova_cdc_") and len(forward) <= 63
    assert forward == forward.lower() and forward.replace("_", "").isalnum()
    assert adapter().name == forward


@pytest.mark.parametrize("conninfo", ["", "   ", None, b"user=postgres"])
def test_the_source_connection_is_required(conninfo):
    with pytest.raises(ContractError):
        adapter(source_conninfo=conninfo)


def test_the_offline_copy_rules_still_apply():
    with pytest.raises(ContractError):
        adapter(tables=())
    with pytest.raises(ContractError):
        adapter(writer_roles=("batch",))  # the application must be a registered writer
