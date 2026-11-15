"""Change collector for GCP resources from Cloud Audit Logs (INV-08, §23.3).

The GCP counterpart of ``AwsCloudTrailCollector``. Each poll reads the Admin
Activity audit log of the project - always on, kept 400 days, and written for
every call that changes configuration or metadata - and records one change
event per successful call on a registered resource: ``policy`` for calls that
change who may do what, ``resource`` for calls that change the resource
itself, ``unclassified`` for any other write, never nothing. A call Google
refused (a non-zero ``status``) changed nothing and is not recorded.

Resources are registered by the ``protoPayload.resourceName`` their audit
entries carry (``projects/_/buckets/<bucket>``, ``projects/<number>/secrets/
<secret>`` ...), and an entry on anything below a registered name counts for
it: a new secret version is logged as ``.../secrets/<secret>/versions/<n>``
(measured), and a collector matching names exactly would miss every rotation.
Entries usually arrive within seconds, without a documented bound, so
the watermark stays ``delivery_lag`` behind now and every poll reads again
from the watermark; an entry is recorded once (its log name, insert ID and
time identify it). Data Access logs (object reads and writes) are not read:
they are off by default and are not catalogue changes.
"""

from datetime import UTC, datetime, timedelta
import re

from sova.adapters.gcp_rest import GcpRest
from sova.domain.change_impact import ChangeEvent, ChangeKind
from sova.domain.contracts import ContractError, identifier, utc

API = "https://logging.googleapis.com/v2/entries:list"
_POLICY = re.compile(
    r"(?:^|\.)(?:SetIamPolicy|setIamPolicy|setIamPermissions|SetIAMPolicy)$"
    r"|^v1\.compute\.firewalls\.(?:insert|patch|update|delete)$"
    r"|^v1\.compute\.(?:securityPolicies)\.")
_RESOURCE = re.compile(
    r"^storage\.buckets\.(?:create|delete|update)$"
    r"|^cloudsql\.instances\.(?:create|delete|restoreBackup|clone|restart|promoteReplica"
    r"|failover|import|stop|start)$"
    r"|^cloudsql\.backupRuns\.(?:create|delete)$"
    r"|^google\.cloud\.secretmanager\.v1\.SecretManagerService\.(?:CreateSecret|DeleteSecret"
    r"|AddSecretVersion|DisableSecretVersion|EnableSecretVersion|DestroySecretVersion)$"
    r"|^google\.cloud\.run\.v[12]\.Services\.(?:CreateService|DeleteService|ReplaceService)$"
    r"|^dns\.(?:managedZones\.(?:create|delete)|changes\.create)$")


def classify(method):
    if _POLICY.search(method or ""):
        return ChangeKind.POLICY
    if _RESOURCE.search(method or ""):
        return ChangeKind.RESOURCE
    return ChangeKind.UNCLASSIFIED  # e.g. cloudsql.instances.update: tier, network and flags alike


def parse_time(text):
    """RFC 3339 with up to nanoseconds, to an aware datetime (microseconds)."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)",
                         text or "")
    if not match:
        raise ContractError("Audit entry without a readable timestamp")
    base, fraction, zone = match.groups()
    value = datetime.fromisoformat(base + ("." + (fraction + "000000")[:6] if fraction else "")
                                   + ("+00:00" if zone == "Z" else zone))
    return value.astimezone(UTC)


def _stamp(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class GcpAuditLogCollector:
    def __init__(self, feed, *, rest, clock, tenant_id, application_id, collector_id,
                 resources, delivery_lag=timedelta(minutes=10), lookback=timedelta(hours=1)):
        """``resources`` maps an audit ``resourceName`` to its (environment, resource UID)."""
        if not all(callable(getattr(feed, n, None)) for n in ("record", "advance")):
            raise ContractError("Change feed required")
        if not isinstance(rest, GcpRest) or not callable(clock):
            raise ContractError("GCP REST access and trusted clock required")
        if type(resources) is not dict or not resources:
            raise ContractError("Resources mapped to (environment, resource UID) required")
        for name, (environment, uid) in resources.items():
            identifier(name, "resource name", limit=2048)
            if '"' in name or "\\" in name:
                raise ContractError("Resource name required")
            identifier(environment, "environment_id")
            identifier(uid, "resource UID")
        if not timedelta(0) < delivery_lag <= timedelta(hours=24):
            raise ContractError("Bounded positive delivery lag required")
        self.feed, self.rest, self.clock = feed, rest, clock
        self.tenant_id = identifier(tenant_id, "tenant_id")
        self.application_id = identifier(application_id)
        self.collector_id = identifier(collector_id, "collector_id")
        self.resources = dict(resources)
        self.delivery_lag, self.lookback = delivery_lag, lookback
        self.marks = {}
        self.seen = {}  # entry ID -> entry time, for entries after the watermark

    def _since(self, environment, now):
        if environment not in self.marks:
            existing = ()
            if callable(getattr(self.feed, "watermarks", None)):
                existing = self.feed.watermarks((environment,))
            found = [m.observed_through for m in existing if m.environment_id == environment]
            self.marks[environment] = max(found) if found else now - self.lookback
        return self.marks[environment]

    def _entries(self, names, since, until):
        log = f"projects/{self.rest.project}/logs/cloudaudit.googleapis.com%2Factivity"
        wanted = " OR ".join(f'protoPayload.resourceName:"{n}"' for n in names)
        body = {"resourceNames": [f"projects/{self.rest.project}"], "orderBy": "timestamp asc",
                "pageSize": 1000,
                "filter": f'logName="{log}" AND timestamp>="{_stamp(since)}" AND '
                          f'timestamp<="{_stamp(until)}" AND ({wanted})'}
        entries = []
        while True:
            page = self.rest.post(API, body) or {}
            entries += page.get("entries", [])
            if len(entries) > 100_000:
                raise ContractError("More audit entries than the bound")
            if not page.get("nextPageToken"):
                return entries
            body["pageToken"] = page["nextPageToken"]

    @staticmethod
    def _owner(name, names):
        """The registered resource ``name`` is, or lies below."""
        found = [n for n in names if name == n or name.startswith(n + "/")]
        return max(found, key=len) if found else None

    def poll(self, *, cause=None):
        """Record the successful write calls since each environment's watermark."""
        now = utc(self.clock())
        recorded = []
        by_environment = {}
        for name, (environment, uid) in sorted(self.resources.items()):
            by_environment.setdefault(environment, []).append(name)
        for environment, names in sorted(by_environment.items()):
            for entry in self._entries(names, self._since(environment, now), now):
                payload = entry.get("protoPayload") or {}
                if (payload.get("status") or {}).get("code"):
                    continue  # Google refused the call: nothing changed
                key = f"{entry.get('timestamp')}:{entry.get('insertId')}"
                if key in self.seen:
                    continue  # read again because it is after the watermark
                owner = self._owner(payload.get("resourceName") or "", names)
                if owner is None:
                    continue  # the filter's substring match also finds look-alikes
                _, uid = self.resources[owner]
                change = ChangeEvent(
                    f"{self.collector_id}:audit:{entry.get('insertId')}", self.tenant_id,
                    self.application_id, classify(payload.get("methodName")), environment,
                    uid, None, parse_time(entry.get("timestamp")), cause)
                self.feed.record(change, collector_id=self.collector_id)
                self.seen[key] = change.observed_at
                recorded.append(change)
        through = now - self.delivery_lag
        for environment in sorted(by_environment):
            if through > self.marks[environment]:
                self.feed.advance(environment, collector_id=self.collector_id,
                                  observed_through=through)
                self.marks[environment] = through
        oldest = min(self.marks.values())
        self.seen = {i: t for i, t in self.seen.items() if t >= oldest}
        return tuple(recorded)
