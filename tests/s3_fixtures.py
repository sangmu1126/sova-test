"""Two disposable S3-compatible stores for object-storage handoff tests.

``SOVA_TEST_S3_ENDPOINTS`` names the source and target stores (comma separated;
one endpoint serves both sides), ``SOVA_TEST_S3_ROOT`` the administrative
``access:secret`` valid on both, and ``SOVA_TEST_S3_WRITERS`` two writer
accounts ``access:secret`` present on both: the application first, then a
second writer path. Only loopback endpoints are accepted, so these tests can
never reach a real cloud account.
"""

import os
from urllib.parse import urlparse

import pytest


def _pair(value):
    access, _, secret = value.partition(":")
    if not access or not secret:
        raise pytest.UsageError("access:secret expected")
    return access, secret


@pytest.fixture(scope="session")
def s3_stores():
    endpoints = os.environ.get("SOVA_TEST_S3_ENDPOINTS")
    if not endpoints:
        pytest.skip("SOVA_TEST_S3_ENDPOINTS is required for real object-storage tests")
    boto3 = pytest.importorskip("boto3")
    import botocore.config

    urls = [u.strip() for u in endpoints.split(",") if u.strip()]
    if len(urls) not in (1, 2) or any(
            urlparse(u).hostname not in ("127.0.0.1", "localhost", "::1") for u in urls):
        raise pytest.UsageError("Object-storage tests run only against loopback stores")
    source_url, target_url = urls[0], urls[-1]
    root = _pair(os.environ["SOVA_TEST_S3_ROOT"])
    writers = [_pair(w) for w in os.environ["SOVA_TEST_S3_WRITERS"].split(",")]
    if len(writers) != 2:
        raise pytest.UsageError("Two writer accounts expected: application, other")
    config = botocore.config.Config(retries={"max_attempts": 1}, connect_timeout=5,
                                    read_timeout=30)

    def client(url, credentials):
        return boto3.client("s3", endpoint_url=url, aws_access_key_id=credentials[0],
                            aws_secret_access_key=credentials[1], region_name="us-east-1",
                            config=config)

    endpoints = {"source": source_url, "target": target_url}
    principals = {"app": writers[0][0], "other": writers[1][0]}
    return {
        "root": lambda side: client(endpoints[side], root),
        "as": lambda side, writer: client(endpoints[side],
                                          writers[0 if writer == "app" else 1]),
        # Logical writer -> this store's principal; the same accounts on both here.
        "principals": {"source": principals, "target": principals},
        "bucket_name": lambda side, suffix: (
            f"orders-{'src' if side == 'source' else 'tgt'}-{suffix}"),
        "create_bucket": lambda c, bucket: c.create_bucket(Bucket=bucket),
    }
