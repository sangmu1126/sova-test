"""Real TCP verifies the unchanged client honors lookup 503 backpressure."""

import asyncio
import json
import os
from pathlib import Path
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import pytest
from qualification_orders import serve

from sova.qualification.bounded_orders import BoundedOrderClient
from sova.qualification.requests import OrderIntent, RequestJournal


@pytest.mark.parametrize("hint", [1, 60])
def test_lookup_hint_paces_requests_or_stops_at_the_existing_deadline(tmp_path, hint):
    app, calls = FastAPI(), []
    def record(request):
        calls.append({"method": request.method, "at_monotonic": time.monotonic()})
    @app.post("/orders")
    async def submit(request: Request):
        record(request)
        if len(calls) == 1:
            return JSONResponse({}, status_code=503, headers={"retry-after": "0"})
        return {"order_id": 1, "customer": "customer", "idempotency_key": "key", "amount": "12.34", "result": "confirmed"}
    @app.get("/orders")
    async def lookup(request: Request):
        record(request)
        return JSONResponse({}, status_code=503 if len(calls) == 2 else 404,
                            headers={"retry-after": str(hint)} if len(calls) == 2 else {})
    with serve(app) as origin:
        journal = RequestJournal.create(tmp_path / "client.sqlite", run_id="wait-hint", tenant_id="tenant", profile_digest="a"*64)
        async def run():
            client = BoundedOrderClient(origin, bearer_token="fixture", journal=journal, jitter=lambda a,b: 0)
            try:
                return await client.complete(OrderIntent("request", "customer", "key", "12.34"))
            finally:
                await client.aclose()
        try:
            result = asyncio.run(run())
            if hint == 1:
                assert result["result"] == "CONFIRMED" and result["attempts"] == 4
                assert [c["method"] for c in calls] == ["POST", "GET", "GET", "POST"]
                assert calls[2]["at_monotonic"] - calls[1]["at_monotonic"] >= 1
                assert [o["status"] for o in journal.snapshot()["outcomes"]] == [503, 503, 404, 200]
            else:
                assert result["result"] == "EXHAUSTED" and result["reason"] == "deadline"
                assert result["attempts"] == 2 and [c["method"] for c in calls] == ["POST", "GET"]
                assert [o["status"] for o in journal.snapshot()["outcomes"]] == [503, 503]
            if directory := os.environ.get("SOVA_TEST_QUALIFICATION_ARTIFACTS"):
                (Path(directory) / ("lookup-tcp-paced.json" if hint == 1 else "lookup-tcp-deadline.json")).write_text(
                    json.dumps({"environment": "real loopback TCP with controlled HTTP responses, no real DB in this case",
                        "lookup_retry_after_seconds": hint, "calls": calls, "result": result,
                        "client_budget_seconds": 5, "max_attempts_including_lookup": 8,
                        "qualification_issued": False, "authorizes_execution": False}, indent=2) + "\n")
        finally:
            journal.close()
