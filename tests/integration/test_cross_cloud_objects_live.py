"""Object storage between two clouds: AWS S3 to Cloud Storage and back.

The same object-storage adapter and conformance kit, with each side fenced
and opened the way its provider works (an S3 bucket policy for IAM roles on
AWS; bucket IAM bindings for service accounts on GCS, judged by Policy
Troubleshooter after the propagation window). Every write the kit attempts
is a real request as a real writer.
"""

import json

from adapter_conformance import HandoffAdapterConformance, apply
from cross_cloud_world import CloudPairWorld
import pytest

from sova.domain.contracts import ContractError

pytestmark = [pytest.mark.aws_live, pytest.mark.gcp_live]
PROPAGATION_SECONDS = 60  # see test_gcp_storage_live


@pytest.fixture(params=["aws", "gcs"], ids=["s3-to-gcs", "gcs-to-s3"])
def pair_world(request, aws_s3_store, gcs_stores):
    world = CloudPairWorld(aws_s3_store, gcs_stores, source=request.param,
                           propagation_seconds=PROPAGATION_SECONDS)
    yield world
    world.cleanup()


class TestCrossCloudObjectConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, pair_world):
        return pair_world


def test_s3_object_tags_are_refused_on_the_way_to_gcs_not_dropped(aws_s3_store, gcs_stores):
    """GCS objects have no tags. An S3 object that carries some cannot be
    represented there; the copy stops with an error instead of moving the
    object without them."""
    world = CloudPairWorld(aws_s3_store, gcs_stores, source="aws",
                           propagation_seconds=PROPAGATION_SECONDS)
    try:
        world._as("source", "app").put_object(
            Bucket=world.buckets["source"], Key="orders/tagged.json",
            Body=json.dumps({"order": "tagged"}).encode(), Tagging="class=invoice")
        for action in ("stage_target", "pause_admission", "fence_source_writes"):
            apply(world.adapter(), action)
        with pytest.raises(ContractError, match="no tags"):
            apply(world.adapter(), "copy_content")
    finally:
        world.cleanup()
