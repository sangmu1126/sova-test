"""Finite same-key completion for the qualification order API.

Requires an authoritative key lookup and atomic idempotent submission. This is
a client measurement policy, not server-side buffering or an exactly-once
transport. Deadlines cancel async network I/O; local disk/observer scheduling
is not a real-time OS guarantee. Every attempt/outcome remains in the journal.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
import random
import time

import httpx

from sova.qualification.http_orders import fixed_origin
from sova.qualification.requests import OrderIntent, receipt_matches


@dataclass(frozen=True)
class CompletionBudget:
    seconds: float = 5.0
    max_attempts: int = 8
    attempt_seconds: float = 1.0
    backoff_seconds: float = .1
    max_backoff_seconds: float = .5

    def __post_init__(self):
        for value in (self.seconds, self.attempt_seconds, self.backoff_seconds, self.max_backoff_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("Finite positive completion limits required")
        if (not self.attempt_seconds <= self.seconds <= 30 or not self.backoff_seconds <= self.max_backoff_seconds <= 5
                or type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 16):
            raise ValueError("Completion budget outside supported limits")


class BoundedOrderClient:
    def __init__(self, origin, *, bearer_token, journal, client=None, budget=CompletionBudget(),
                 observer=None, jitter=None):
        self.origin = fixed_origin(origin)
        if not isinstance(bearer_token, str) or not bearer_token or any(c.isspace() for c in bearer_token):
            raise ValueError("A bearer token is required")
        if not isinstance(budget, CompletionBudget):
            raise ValueError("A typed completion budget is required")
        self.token, self.journal, self.budget = bearer_token, journal, budget
        self.client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self.owned = client is None
        self.observer = observer
        self.jitter = jitter or random.SystemRandom().uniform

    async def aclose(self):
        if self.owned:
            await self.client.aclose()

    async def _attempt(self, intent, kind, seconds, sequence, deadline):
        self.journal.request(intent)
        attempt = self.journal.begin(intent.request_id, kind=kind)
        status, error, receipt, retry_after, side = None, None, None, None, None
        start, at = time.monotonic(), datetime.now(UTC)
        transport_invoked = False
        # Durably recording intent/attempt can itself consume the remaining
        # budget. Recheck afterwards instead of starting a fresh full timeout.
        seconds = min(seconds, deadline - start)
        headers = {"authorization": "Bearer " + self.token, "idempotency-key": intent.idempotency_key,
                   "x-qualification-tenant": self.journal.scope["tenant_id"],
                   "x-qualification-run": self.journal.scope["run_id"]}
        kwargs = ({"json": {"customer": intent.customer, "amount": intent.amount}} if kind == "submit" else
                  {"params": {"customer": intent.customer, "idempotency_key": intent.idempotency_key}})
        try:
            if seconds <= 0:
                raise TimeoutError()
            # Absolute timeout includes response-body streaming, not just a
            # per-read idle timeout that a slow byte stream could keep alive.
            async with asyncio.timeout(seconds):
                transport_invoked = True
                async with self.client.stream("POST" if kind == "submit" else "GET", self.origin + "/orders",
                                              headers=headers, follow_redirects=False, timeout=seconds, **kwargs) as response:
                    status = response.status_code
                    side = response.headers.get("x-qualification-side")
                    raw = response.headers.get("retry-after")
                    if raw is not None:
                        retry_after = int(raw) if len(raw) <= 5 and raw.isascii() and raw.isdecimal() else "unsupported"
                    if 200 <= status < 300:
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 16384:
                                break
                        try:
                            parsed = json.loads(data) if len(data) <= 16384 else None
                        except (ValueError, UnicodeError):
                            parsed = None
                        request = {"customer": intent.customer, "idempotency_key": intent.idempotency_key, "amount": intent.amount}
                        if receipt_matches(parsed, request) and status in (200, 201):
                            receipt = parsed
                        else:
                            error = "invalid_receipt"
        except (httpx.HTTPError, TimeoutError):
            status, error, receipt = None, "transport_error", None
        except asyncio.CancelledError:
            self.journal.finish(attempt, error="transport_error")
            raise
        self.journal.finish(attempt, status=status, error=error, receipt=receipt)
        if self.observer:
            self.observer({"request_id": intent.request_id, "kind": kind, "sequence": sequence,
                "started_at": at.isoformat(), "finished_at": datetime.now(UTC).isoformat(),
                "elapsed_ms": round((time.monotonic() - start) * 1000, 3), "status": status,
                "error": error, "transport_invoked": transport_invoked,
                "side": side if side in ("source", "target") else None})
        return status, error, receipt, retry_after, transport_invoked

    async def complete(self, intent):
        if not isinstance(intent, OrderIntent):
            raise ValueError("A typed order intent is required")
        start, kind, attempts, delay, transport_calls = time.monotonic(), "submit", 0, 0.0, 0
        result, reason = "EXHAUSTED", "attempt_limit"
        while attempts < self.budget.max_attempts:
            remaining = self.budget.seconds - (time.monotonic() - start)
            if remaining <= delay:
                reason = "deadline"
                break
            if delay:
                await asyncio.sleep(delay)
            remaining = self.budget.seconds - (time.monotonic() - start)
            if remaining <= 0:
                reason = "deadline"
                break
            attempts += 1
            status, error, receipt, retry_after, invoked = await self._attempt(
                intent, kind, min(remaining, self.budget.attempt_seconds), attempts, start+self.budget.seconds)
            transport_calls += invoked
            if receipt is not None:
                result, reason = "CONFIRMED", "validated_receipt"
                break
            if error == "invalid_receipt":
                result, reason = "REJECTED", "invalid_receipt"
                break
            if retry_after == "unsupported":
                result, reason = "REJECTED", "unsupported_retry_after"
                break
            if kind == "lookup" and status == 404:
                kind, delay = "submit", retry_after or 0.0
                continue
            if status not in (None, 503):
                result, reason = "REJECTED", "nonretryable_status"
                break
            # An ambiguous POST is never blindly resent. First observe its
            # business key; only a definitive 404 permits another submission.
            kind = "lookup"
            ceiling = min(self.budget.max_backoff_seconds, self.budget.backoff_seconds * 2**(attempts - 1))
            sampled = self.jitter(0, ceiling)
            if type(sampled) not in (int, float) or not math.isfinite(sampled) or not 0 <= sampled <= ceiling:
                raise ValueError("Invalid retry jitter")
            delay = max(sampled, retry_after or 0)
        return {"request_id": intent.request_id, "result": result, "reason": reason, "attempts": attempts,
                "elapsed_ms": round((time.monotonic() - start) * 1000, 3), "transport_calls": transport_calls,
                "qualification_issued": False, "authorizes_execution": False}
