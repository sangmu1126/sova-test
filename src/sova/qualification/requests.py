"""A durable client journal and an independent order-result comparison.

This is a local measurement record, not signed production evidence or WORM.
SQLite FULL commits record intent before transmission. Reopening starts a new
clock domain: elapsed times across process instances are deliberately unknown.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError("A bounded qualification identifier is required")
    return value


def amount(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,2})?", value):
        raise ValueError("A positive bounded decimal amount is required")
    if Decimal(value) <= 0:
        raise ValueError("A positive bounded decimal amount is required")
    return format(Decimal(value), ".2f")


@dataclass(frozen=True)
class OrderIntent:
    request_id: str
    customer: str
    idempotency_key: str
    amount: str

    def __post_init__(self):
        for value in (self.request_id, self.customer, self.idempotency_key):
            identifier(value)
        object.__setattr__(self, "amount", amount(self.amount))


def receipt_matches(receipt, request):
    """No coercion: another customer's response must never confirm this order."""
    if not isinstance(receipt, dict) or set(receipt) != {
        "order_id", "customer", "idempotency_key", "amount", "result"
    }:
        return False
    if type(receipt["order_id"]) is not int or not 0 < receipt["order_id"] < 2**63:
        return False
    try:
        return (receipt["customer"] == request["customer"]
                and receipt["idempotency_key"] == request["idempotency_key"]
                and amount(receipt["amount"]) == request["amount"]
                and receipt["result"] == "confirmed")
    except (ValueError, TypeError):
        return False


_SCHEMA = """
CREATE TABLE metadata (document TEXT NOT NULL);
CREATE TABLE requests (
 request_id TEXT PRIMARY KEY, customer TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 amount TEXT NOT NULL, clock_id TEXT NOT NULL, created_ns INTEGER NOT NULL,
 UNIQUE(customer, idempotency_key));
CREATE TABLE attempts (
 attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(request_id),
 kind TEXT NOT NULL CHECK(kind IN ('submit','lookup')), clock_id TEXT NOT NULL,
 started_ns INTEGER NOT NULL);
CREATE TABLE outcomes (
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), ended_ns INTEGER NOT NULL,
 status INTEGER, error TEXT, receipt TEXT);
"""


class RequestJournal:
    """Single-tenant/run SQLite file in an owner-controlled local directory.

    It contains generated test identifiers and amounts, never credentials,
    response bodies, endpoint URLs or exception messages. Its owner can still
    edit the file; use the existing evidence signer for operational publication.
    """

    def __init__(self, path, *, clock=time.monotonic_ns):
        self.path = Path(path)
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("An existing regular journal file is required")
        self.clock, self.clock_id = clock, uuid4().hex
        self._last_tick = None
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        try:
            rows = self.db.execute("SELECT document FROM metadata").fetchall()
            if len(rows) != 1:
                raise ValueError("Invalid journal metadata")
            self.scope = json.loads(rows[0][0])
            if set(self.scope) != {"version", "run_id", "tenant_id", "profile_digest"}:
                raise ValueError("Invalid journal metadata")
            if type(self.scope["version"]) is not int or self.scope["version"] != 1:
                raise ValueError("Unsupported journal version")
            identifier(self.scope["run_id"])
            identifier(self.scope["tenant_id"])
            if not re.fullmatch(r"[0-9a-f]{64}", self.scope["profile_digest"]):
                raise ValueError("Invalid profile digest")
        except Exception:
            self.db.close()
            raise

    @classmethod
    def create(cls, path, *, run_id, tenant_id, profile_digest, clock=time.monotonic_ns):
        scope = {"version": 1, "run_id": identifier(run_id),
                 "tenant_id": identifier(tenant_id), "profile_digest": profile_digest}
        if not isinstance(profile_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", profile_digest):
            raise ValueError("A SHA-256 profile digest is required")
        # Never overwrite a previous run or follow a user-provided symlink.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(path) as db:
            db.executescript(_SCHEMA)
            db.execute("INSERT INTO metadata VALUES (?)", (json.dumps(scope, sort_keys=True),))
            for table in ("metadata", "requests", "attempts", "outcomes"):
                for verb in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER {table}_{verb} BEFORE {verb} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'append only'); END")
        return cls(path, clock=clock)

    def close(self):
        self.db.close()

    def _now(self):
        value = self.clock()
        if type(value) is not int or not 0 <= value < 2**63:
            raise ValueError("Invalid monotonic timestamp")
        if self._last_tick is not None and value < self._last_tick:
            raise ValueError("Monotonic clock regressed")
        self._last_tick = value
        return value

    def request(self, intent):
        if not isinstance(intent, OrderIntent):
            raise ValueError("A typed order intent is required")
        document = asdict(intent)
        with self.db:
            found = self.db.execute("SELECT * FROM requests WHERE request_id=?",
                                    (intent.request_id,)).fetchone()
            if found:
                if any(found[k] != v for k, v in document.items()):
                    raise ValueError("Request ID already binds another intent")
                return
            same_key = self.db.execute(
                "SELECT amount FROM requests WHERE customer=? AND idempotency_key=?",
                (intent.customer, intent.idempotency_key)).fetchall()
            if any(r[0] != intent.amount for r in same_key):
                raise ValueError("Idempotency key already binds another amount")
            if same_key:
                raise ValueError("Business key already belongs to another logical request")
            self.db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?)",
                            (*document.values(), self.clock_id, self._now()))

    def begin(self, request_id, *, kind="submit"):
        if kind not in ("submit", "lookup"):
            raise ValueError("Unknown attempt kind")
        attempt_id = uuid4().hex
        start = self._now()
        request = self.db.execute("SELECT clock_id,created_ns FROM requests WHERE request_id=?",
                                  (identifier(request_id),)).fetchone()
        if request is None:
            raise ValueError("Intent must be persisted before transmission")
        if kind == "lookup" and self.db.execute(
                "SELECT 1 FROM attempts WHERE request_id=? AND kind='submit' LIMIT 1",
                (request_id,)).fetchone() is None:
            raise ValueError("Lookup requires a previously recorded submission attempt")
        if request["clock_id"] == self.clock_id and start < request["created_ns"]:
            raise ValueError("Monotonic clock regressed")
        with self.db:
            self.db.execute("INSERT INTO attempts VALUES (?,?,?,?,?)",
                            (attempt_id, request_id, kind, self.clock_id, start))
        return attempt_id

    def finish(self, attempt_id, *, status=None, error=None, receipt=None):
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise ValueError("Invalid HTTP status")
        if error not in (None, "transport_error", "invalid_receipt"):
            raise ValueError("Only redacted error categories may be recorded")
        if status is None and error != "transport_error":
            raise ValueError("An HTTP status or transport failure is required")
        if error == "transport_error" and status is not None:
            raise ValueError("A transport error cannot assert an HTTP response")
        if error == "invalid_receipt" and (status is None or not 200 <= status < 300):
            raise ValueError("An invalid receipt must describe a successful response")
        row = self.db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None or row["clock_id"] != self.clock_id:
            raise ValueError("Attempt must finish in its original clock domain")
        ended = self._now()
        if ended < row["started_ns"]:
            raise ValueError("Monotonic clock regressed")
        request = dict(self.db.execute("SELECT * FROM requests WHERE request_id=?",
                                      (row["request_id"],)).fetchone())
        if receipt is not None and not receipt_matches(receipt, request):
            raise ValueError("Receipt does not match the request")
        if receipt is not None and (status != 200 and status != 201 or error is not None):
            raise ValueError("A receipt needs a successful HTTP outcome")
        with self.db:
            self.db.execute("INSERT INTO outcomes VALUES (?,?,?,?,?)",
                            (attempt_id, ended, status, error,
                             json.dumps(receipt, sort_keys=True) if receipt is not None else None))

    def snapshot(self):
        def rows(table):
            return [dict(r) for r in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        # A single read transaction prevents a concurrent append yielding half an attempt.
        self.db.execute("BEGIN")
        try:
            result = {"scope": dict(self.scope), "requests": rows("requests"),
                      "attempts": rows("attempts"), "outcomes": rows("outcomes")}
            for r in result["outcomes"]:
                r["receipt"] = json.loads(r["receipt"]) if r["receipt"] is not None else None
            return result
        finally:
            self.db.rollback()


def reconcile(journal, observation):
    """Compare a client journal to a complete independent final DB observation.

    No operational PASS, SLO or execution authority is issued. A committed
    order without a valid client acknowledgement/lookup remains unresolved.
    """
    scope = journal["scope"]
    observed_scope = observation.get("scope")
    if (not isinstance(observed_scope, dict) or type(observed_scope.get("version")) is not int
            or observed_scope != scope or type(observation.get("complete")) is not bool):
        raise ValueError("Observation must bind the exact run, tenant and profile")
    requests = journal["requests"]
    if len({r["request_id"] for r in requests}) != len(requests):
        raise ValueError("Duplicate request IDs")
    attempts = journal["attempts"]
    if len({a["attempt_id"] for a in attempts}) != len(attempts):
        raise ValueError("Duplicate attempt IDs")
    by_request = {r["request_id"]: [] for r in requests}
    outcome_by_id = {}
    for out in journal["outcomes"]:
        if out["attempt_id"] in outcome_by_id:
            raise ValueError("Duplicate outcomes")
        outcome_by_id[out["attempt_id"]] = out
    if set(outcome_by_id) - {a["attempt_id"] for a in attempts}:
        raise ValueError("Outcome has no attempt")
    for a in attempts:
        if a["request_id"] not in by_request or a["kind"] not in ("submit", "lookup"):
            raise ValueError("Attempt has no request or has an invalid kind")
        by_request[a["request_id"]].append(a)
    results, violations = [], []
    initial_http_errors = transport_errors = submissions = successful_responses = pending = 0
    for request in requests:
        intent = OrderIntent(**{k: request[k] for k in OrderIntent.__dataclass_fields__})
        matching = [r for r in observation["orders"] if
                    (r["customer"], r["idempotency_key"]) == (intent.customer, intent.idempotency_key)]
        local_violations, confirmations, domains = [], [], {request["clock_id"]}
        acknowledged, sent, missing_outcomes = False, False, False
        for a in by_request[intent.request_id]:
            domains.add(a["clock_id"])
            sent |= a["kind"] == "submit"
            out = outcome_by_id.get(a["attempt_id"])
            if a["kind"] == "submit":
                submissions += 1
            if out is None:
                pending += 1
                missing_outcomes = True
                continue
            if type(out["ended_ns"]) is not int or out["ended_ns"] < a["started_ns"]:
                raise ValueError("Invalid attempt duration")
            status = out["status"]
            if status is not None and (type(status) is not int or not 100 <= status <= 599):
                raise ValueError("Invalid outcome HTTP status")
            success = type(status) is int and 200 <= status < 300
            acknowledged |= success
            if a["kind"] == "submit":
                transport_errors += status is None
                initial_http_errors += status is not None and not success
                successful_responses += success
            if success:
                if out["error"] or not receipt_matches(out["receipt"], request):
                    local_violations.append("INVALID_SUCCESS_RECEIPT")
                else:
                    confirmations.append((a["clock_id"], out["ended_ns"], out["receipt"]["order_id"]))
        if len(matching) > 1:
            local_violations.append("DUPLICATE_BUSINESS_RESULT")
        if matching:
            row = matching[0]
            if not receipt_matches({k: row.get(k) for k in
                                    ("order_id", "customer", "idempotency_key", "amount", "result")}, request):
                local_violations.append("BUSINESS_RESULT_MISMATCH")
            if (type(row.get("work_items")) is not int or row["work_items"] != 1
                    or type(row.get("done_items")) is not int or row["done_items"] != 1):
                local_violations.append("BUSINESS_EFFECT_MISMATCH")
            if any(c[2] != row["order_id"] for c in confirmations):
                local_violations.append("RECEIPT_ORDER_MISMATCH")
        elif acknowledged and observation["complete"]:
            local_violations.append("ACKNOWLEDGED_RESULT_MISSING")
        known = observation["complete"]
        if local_violations:
            resolution = "VIOLATION"
        elif not known:
            resolution = "UNKNOWN"
        elif not sent:
            resolution = "UNKNOWN"
        elif matching and confirmations:
            resolution = "CONFIRMED"
        elif matching:
            resolution = "UNRESOLVED_COMMIT"
        elif not sent or missing_outcomes:
            resolution = "UNKNOWN"
        else:
            resolution = "NOT_COMMITTED"
        elapsed = None
        if resolution == "CONFIRMED" and len(domains) == 1:
            first_confirmation = min(c[1] for c in confirmations)
            if first_confirmation < request["created_ns"]:
                raise ValueError("Confirmation precedes intent")
            elapsed = (first_confirmation - request["created_ns"]) / 1_000_000
        results.append({"request_id": intent.request_id, "resolution": resolution,
                        "total_completion_ms": elapsed, "violations": sorted(set(local_violations))})
        violations.extend(local_violations)
    integrity = ("VIOLATION" if violations else
                 "SATISFIED" if requests and observation["complete"]
                 and not any(r["resolution"] == "UNKNOWN" for r in results) else "UNKNOWN")
    return {"version": 1, "scope": scope, "data_integrity": integrity,
            "service_continuity": "NOT_QUALIFIED", "qualification_issued": False,
            "authorizes_execution": False, "requests": results,
            "counts": {"logical_requests": len(requests), "submission_attempts": submissions,
                       "successful_submission_responses": successful_responses,
                       "http_error_attempts": initial_http_errors,
                       "transport_error_attempts": transport_errors, "unfinished_attempts": pending,
                       "confirmed_requests": sum(r["resolution"] == "CONFIRMED" for r in results),
                       "unresolved_commits": sum(r["resolution"] == "UNRESOLVED_COMMIT" for r in results),
                       "not_committed_requests": sum(r["resolution"] == "NOT_COMMITTED" for r in results)}}
