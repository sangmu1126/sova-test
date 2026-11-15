"""Google Cloud Storage (the GCP source) to an S3-compatible store on premises.

The same object-storage adapter and conformance kit as between two local
gateways and from AWS S3, with a GCS bucket as the source: its writers are
two run-scoped service accounts whose bucket grants are the only ones they
hold, the fence removes those grants, and Policy Troubleshooter decides
whether each writer can still write. Every write the kit attempts is real.
"""

from datetime import UTC, datetime, timedelta

from adapter_conformance import HandoffAdapterConformance, apply, readback
from gcp_fixtures import gcloud
from gcs_world import GcsWorld
import pytest

from sova.adapters.gcs_store import UPLOAD
from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.gcp_live, pytest.mark.s3]
# IAM revocation measured here reached every front end within 4.4 s (194 probes);
# the tests wait 60 s. Production keeps the default, the 7 minutes Google names.
PROPAGATION_SECONDS = 60


@pytest.fixture
def gcs_world(gcs_stores, s3_stores):
    world = GcsWorld(gcs_stores, s3_stores, propagation_seconds=PROPAGATION_SECONDS)
    yield world
    world.cleanup()


class TestGcsToOnPremConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, gcs_world):
        return gcs_world


def test_a_resumable_upload_opened_before_the_fence_cannot_complete_after_it(gcs_world):
    """Resumable sessions cannot be listed, so the fence must not depend on
    seeing them: GCS checks the uploader's permission when the session
    completes, and a fenced writer's session is refused."""
    world = gcs_world
    session = world.g["as"]["app"].session
    started = session.request(
        "POST", f"{UPLOAD}/b/{world.buckets['source']}/o",
        params={"uploadType": "resumable"}, json={"name": "orders/late.json"}, timeout=30)
    assert started.status_code == 200
    location = started.headers["Location"]
    for action in ("stage_target", "pause_admission", "fence_source_writes"):
        apply(world.adapter(), action)
    assert readback(world.adapter(), "fence_source_writes") == EffectStatus.APPLIED
    assert not world.can_write("source", "app")
    import requests

    late = requests.put(location, data=b'{"order": "late"}', timeout=30)  # the URI is the auth
    assert late.status_code == 403, late.status_code
    assert "orders/late.json" not in [o["Key"] for page in world.root["source"].get_paginator(
        "list_objects_v2").paginate(Bucket=world.buckets["source"]) for o in page["Contents"]]


def test_a_writer_with_a_project_grant_keeps_the_fence_unconfirmed(gcp, gcs_world):
    """The fence removes bucket grants only; a project-level grant it cannot
    remove leaves the writer able to write, and the readback says so."""
    world = gcs_world
    member = world.g["members"]["other"]
    gcloud(gcp, "projects", "add-iam-policy-binding", gcp["project"], f"--member={member}",
           "--role=roles/storage.objectAdmin", "--condition=None")
    try:
        for action in ("stage_target", "pause_admission", "fence_source_writes"):
            apply(world.adapter(), action)
        assert readback(world.adapter(), "fence_source_writes") == EffectStatus.UNKNOWN
        holds, detail = world.adapter()._check_fence_source_writes()
        assert detail["source_writers_fenced"] == {"app": True, "other": False}
    finally:
        gcloud(gcp, "projects", "remove-iam-policy-binding", gcp["project"],
               f"--member={member}", "--role=roles/storage.objectAdmin", "--all")
    started = datetime.now(UTC)
    while readback(world.adapter(), "fence_source_writes") != EffectStatus.APPLIED:
        assert datetime.now(UTC) - started < timedelta(minutes=3)
