"""Read-only operator-bound measurements; never handoff admission evidence.

No request-selected file, URL, AWS account, run or credential. Configuration
and measurements are private operator files. Typed projections are scoped to
one registered application. Expired samples remain historical; malformed or
missing records are UNKNOWN. These records never satisfy execution gates.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path

from sova.adapters.kubernetes.https import private_text
from sova.domain.contracts import ContractError, identifier, utc
from sova.domain.json_documents import json_document


class OperationalReadModels:
    def __init__(self, entries, *, clock=lambda: datetime.now(UTC)):
        if (
            type(entries) is not list
            or not 1 <= len(entries) <= 128
            or not callable(clock)
        ):
            raise ContractError("Bounded operator read-model registrations required")
        self.entries, self.clock = {}, clock
        for row in entries:
            if type(row) is not dict or set(row) != {
                "tenant_id",
                "application_id",
                "health",
                "cost",
            }:
                raise ContractError("Exact read-model registration fields required")
            key = tuple(identifier(row[k], k) for k in ("tenant_id", "application_id"))
            if key in self.entries:
                raise ContractError("Duplicate application read-model registration")
            for kind, fields in (
                ("health", {"file", "producer_sha256"}),
                ("cost", {"file", "account_id", "run_id"}),
            ):
                spec = row[kind]
                if spec is None:
                    continue
                if (
                    type(spec) is not dict
                    or set(spec) != fields
                    or type(spec["file"]) is not str
                    or not Path(spec["file"]).is_absolute()
                ):
                    raise ContractError(
                        "Exact operator-selected observation binding required"
                    )
                if kind == "health":
                    from sova.domain.contracts import digest

                    digest(spec["producer_sha256"])
                else:
                    if (
                        type(spec["account_id"]) is not str
                        or len(spec["account_id"]) != 12
                        or not spec["account_id"].isascii()
                        or not spec["account_id"].isdigit()
                    ):
                        raise ContractError("Registered cost account required")
                    identifier(spec["run_id"])
            self.entries[key] = row

    @classmethod
    def from_file(cls, path, **kwargs):
        value = json_document(private_text(path, limit=65536), 65536)
        if (
            type(value) is not dict
            or set(value) != {"version", "applications"}
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise ContractError("Read-model configuration version 1 required")
        return cls(value["applications"], **kwargs)

    @staticmethod
    def _load(path):
        raw = private_text(path, limit=1048576)
        return json_document(raw, 1048576), hashlib.sha256(raw.encode()).hexdigest()

    def read(self, tenant, application):
        key = (identifier(tenant), identifier(application))
        now = utc(self.clock())
        out = {
            "version": 1,
            "tenant_id": tenant,
            "application_id": application,
            "read_at": now.isoformat(),
            "observational_only": True,
            "authorizes_execution": False,
        }
        entry = self.entries.get(key)
        for kind in ("health", "cost"):
            spec = entry[kind] if entry else None
            if spec is None:
                out[kind] = {
                    "state": "unavailable",
                    "reason": "collector_not_configured",
                }
                continue
            try:
                raw, digest = self._load(spec["file"])
                observed = utc(
                    datetime.fromisoformat(
                        raw["completed_at"] if kind == "health" else raw["observed_at"]
                    )
                )
                until = (
                    utc(datetime.fromisoformat(raw["valid_until"]))
                    if kind == "health"
                    else observed + timedelta(minutes=5)
                )
                if (
                    not observed <= now
                    or not observed < until
                    or until - observed > timedelta(minutes=5)
                ):
                    raise ContractError("Invalid bounded measurement interval")
                frame = {
                    "state": "fresh" if now < until else "historical",
                    "observed_at": observed.isoformat(),
                    "valid_until": until.isoformat(),
                    "record_sha256": digest,
                }
                if kind == "health":
                    if (
                        raw.get("passed") is not True
                        or (
                            raw["tenant_id"],
                            raw["application_id"],
                            raw["gateway_source_sha256"],
                        )
                        != (tenant, application, spec["producer_sha256"])
                        or raw["application_server_instrumented"] is not False
                        or raw["slo_qualified"] is not False
                    ):
                        raise ContractError(
                            "Health measurement scope or provenance differs"
                        )
                    rows = raw["actual_http_receipts"]
                    if type(rows) is not list or not 1 <= len(rows) <= 64:
                        raise ContractError("Bounded health samples required")
                    samples = []
                    for r in rows:
                        duration = r["duration_ms"]
                        status = r["status"]
                        if (
                            r["side"] not in ("source", "target")
                            or type(duration) not in (int, float)
                            or not math.isfinite(duration)
                            or not 0 <= duration <= 60000
                            or type(status) is not int
                            or not 100 <= status <= 599
                            or type(r["verified_application_https"]) is not bool
                        ):
                            raise ContractError("Invalid health sample")
                        samples.append(
                            {
                                "side": r["side"],
                                "status": status,
                                "duration_ms": duration,
                                "verified_application_https": r[
                                    "verified_application_https"
                                ],
                            }
                        )
                    frame.update(
                        samples=samples,
                        measurement_scope="controller_health_gateway",
                        application_server_instrumented=False,
                        slo_qualified=False,
                    )
                else:
                    if (
                        (raw["account_id"], raw["run_id"])
                        != (spec["account_id"], spec["run_id"])
                        or raw["complete_tco"] is not False
                        or raw["comparison_authorized"] is not False
                        or type(raw["available"]) is not bool
                    ):
                        raise ContractError("Cost observation scope differs")
                    if (
                        raw.get("estimated") is not None
                        and type(raw["estimated"]) is not bool
                    ):
                        raise ContractError("Boolean cost estimate flag required")
                    if raw.get("reason") not in (
                        None,
                        "allocation_tag_not_active",
                        "billing_data_unavailable",
                        "billing_interval_incomplete",
                        "estimated_zero_not_verified",
                    ):
                        raise ContractError("Known bounded cost reason required")
                    amount = raw["amount"]
                    if raw["available"]:
                        if (
                            type(amount) is not str
                            or len(amount) > 64
                            or raw["currency"] != "USD"
                        ):
                            raise ContractError(
                                "Bounded attributed USD amount required"
                            )
                        value = Decimal(amount)
                        if (
                            not value.is_finite()
                            or abs(value) > Decimal("1e18")
                            or (value == 0 and raw["estimated"] is not False)
                        ):
                            raise ContractError("Unverified cost amount")
                    elif amount is not None:
                        raise ContractError(
                            "Unavailable cost cannot carry a complete amount"
                        )
                    if not raw["available"] and raw["currency"] is not None:
                        raise ContractError(
                            "Unavailable amount has no attributed currency"
                        )
                    frame.update(
                        available=raw["available"],
                        amount=amount,
                        currency=raw["currency"],
                        estimated=raw.get("estimated"),
                        reason=raw.get("reason"),
                        complete_tco=False,
                        comparison_authorized=False,
                    )
                out[kind] = frame
            except (
                OSError,
                ContractError,
                KeyError,
                TypeError,
                ValueError,
                InvalidOperation,
            ):
                out[kind] = {
                    "state": "unavailable",
                    "reason": "collector_record_invalid_or_unreadable",
                }
        return out
