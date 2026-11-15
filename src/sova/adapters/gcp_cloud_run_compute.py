"""Source compute on Google Cloud Run: stop it and read back that it stopped (§20.1).

The Cloud Run counterpart of ``EcsServiceCompute``. A Cloud Run service with
automatic scaling starts an instance for any request that arrives, so "no
instance is running right now" says nothing about whether the source
application can still run. What stops it is manual scaling with an instance
count of zero: Cloud Run then starts no instance for any revision of the
service and answers every request with 503 (measured).

* ``stop_source_compute`` sets that scaling (idempotent), guarded by the
  service's etag so a concurrent change is not overwritten, and waits,
  bounded, until Cloud Run has reconciled it;
* the readback holds only if the service is in manual scaling with zero
  instances, the generation Cloud Run has observed is the one it stores, it
  is not reconciling, and its terminal condition succeeded.

``endpoints()`` gives the service's URLs, so a probe can later try the old
address and confirm the application does not answer there. Deleting the
service, its revisions and images is retirement, a separate approved step.
"""

import re
import time

from sova.adapters.gcp_rest import GcpRest
from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

API = "https://run.googleapis.com/v2"
ACTIONS = frozenset({"stop_source_compute"})
_NAME = re.compile(r"[a-z](?:[a-z0-9-]{0,47}[a-z0-9])?")
_REGION = re.compile(r"[a-z]+-[a-z]+[0-9]")


class CloudRunServiceCompute:
    def __init__(self, adapter_id, *, rest, region, service, stop_wait_seconds=180.0):
        if not isinstance(rest, GcpRest):
            raise ContractError("GCP REST access required")
        if type(region) is not str or not _REGION.fullmatch(region):
            raise ContractError("Cloud Run region required")
        if type(service) is not str or not _NAME.fullmatch(service):
            raise ContractError("Registered Cloud Run service name required")
        self.adapter_id, self.rest = adapter_id, rest
        self.region, self.service = region, service
        self.stop_wait_seconds = stop_wait_seconds
        self.url = (f"{API}/projects/{rest.project}/locations/{region}/services/{service}")

    def _read(self):
        found = self.rest.get(self.url, missing_ok=True)
        if found is None:
            raise ContractError("Registered Cloud Run service not found")
        return found

    def observe(self):
        service = self._read()
        scaling = service.get("scaling") or {}
        terminal = (service.get("terminalCondition") or {}).get("state")
        manual_zero = (scaling.get("scalingMode") == "MANUAL"
                       and int(scaling.get("manualInstanceCount") or 0) == 0)
        settled = (not service.get("reconciling")
                   and str(service.get("observedGeneration")) == str(service.get("generation")))
        stopped = manual_zero and settled and terminal == "CONDITION_SUCCEEDED"
        return stopped, {"service": self.service, "region": self.region,
                         "scaling_mode": scaling.get("scalingMode", "AUTOMATIC"),
                         "manual_instances": scaling.get("manualInstanceCount"),
                         "generation": service.get("generation"),
                         "observed_generation": service.get("observedGeneration"),
                         "reconciling": bool(service.get("reconciling")),
                         "terminal": terminal,
                         "latest_ready_revision": (service.get("latestReadyRevision") or ""
                                                   ).rsplit("/", 1)[-1]}

    def endpoints(self):
        """The service's URLs (the default one and any others it answers on)."""
        service = self._read()
        return sorted(set(service.get("urls") or ()) | ({service["uri"]}
                                                        if service.get("uri") else set()))

    # -- EffectAdapter -------------------------------------------------------
    def validate(self, command):
        if command.adapter_id != self.adapter_id or command.action not in ACTIONS:
            raise ContractError("Unsupported compute action for this adapter")

    def apply(self, command):
        service = self._read()
        scaling = service.get("scaling") or {}
        if not (scaling.get("scalingMode") == "MANUAL"
                and int(scaling.get("manualInstanceCount") or 0) == 0):
            self.rest.patch(self.url, {
                "scaling": {"scalingMode": "MANUAL", "manualInstanceCount": 0},
                "etag": service.get("etag")}, params={"updateMask": "scaling"})
        deadline = time.monotonic() + self.stop_wait_seconds
        while not self.observe()[0] and time.monotonic() < deadline:
            time.sleep(3)

    def readback(self, command, now):
        stopped, detail = self.observe()
        return EffectObservation(
            command.scope, command.fingerprint,
            EffectStatus.APPLIED if stopped else EffectStatus.UNKNOWN, now,
            canonical_json({"action": command.action, **detail}, limit=65536))
