"""Continuous HTTPS business requests through the real Service during recovery."""

import pytest
from qualification_pod_http import HttpTraffic
from test_order_http_handoff import http_business as http_business  # noqa: F401
from test_worker_pod_journey import worker_pod_journey

pytestmark = [pytest.mark.postgres, pytest.mark.kubernetes]


def test_https_orders_and_every_failure_reconcile_after_real_worker_pod_recovery(
        postgres_database, http_business, tmp_path):
    worker_pod_journey(postgres_database, http_business, tmp_path, traffic_factory=HttpTraffic)
