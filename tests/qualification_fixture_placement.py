"""Declared category inventory ONLY for local workflow fixtures.

No physical geography, all-copies discovery, backup location or live collector
is measured here. Production data_measure still refuses a missing observer.
These fixed fixture facts let SQL/worker tests exercise later steps without
pretending a PostgreSQL comment proves audit/backup/AI residence.
"""

FIXTURE_ZONES = {
    "business_data": frozenset({"approved-zone-a"}),
    "backups": frozenset({"approved-zone-a"}),
    "audit": frozenset({"approved-zone-a"}),
}


class FixturePlacement:
    def __init__(self, tenant, application, *, control_connect):
        with control_connect() as conn:
            identity = conn.execute("SELECT current_setting('cluster_name'),current_database()").fetchone()
        if identity != ("sova-persistence-test", "sova_test"):
            raise ValueError("Declared fixture placement requires the isolated test control database")
        self.tenant, self.application = tenant, application

    def __call__(self, category, allowed):
        zones = FIXTURE_ZONES.get(category)
        return bool(zones and zones <= allowed), {
            "probe": "declared_fixture_placement", "fixture_only": True,
            "operational_qualification": False, "tenant_id": self.tenant,
            "application_id": self.application, "category": category,
            "declared_fixture_zones": sorted(zones or ()),
        }


def run_local_handoff(*args, **kwargs):
    """Explicit local SQL regression wrapper. Live/cloud callers must supply
    their own independent category observer instead of importing this wrapper.
    """
    from handoff_drive import run_handoff

    def placement(h):
        return FixturePlacement(h["tenant"], h["app"], control_connect=h["connect"])

    return run_handoff(*args, **kwargs, placement_factory=placement)
