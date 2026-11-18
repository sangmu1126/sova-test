"""A real TCP response that stalls mid-body must not extend the retry budget."""

import asyncio
import time

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from qualification_orders import serve

from sova.qualification.bounded_orders import BoundedOrderClient, CompletionBudget
from sova.qualification.requests import OrderIntent, RequestJournal


def test_streaming_response_is_cancelled_by_absolute_network_deadline(tmp_path):
    app = FastAPI()
    @app.post("/orders")
    async def submit():
        async def body():
            yield b'{"order_id":'
            await asyncio.sleep(10)
            yield b'1}'
        return StreamingResponse(body(),media_type="application/json")
    with serve(app) as origin:
        journal = RequestJournal.create(tmp_path/"requests.sqlite",run_id="slow-body",tenant_id="tenant-a",profile_digest="a"*64)
        async def run():
            client = BoundedOrderClient(origin,bearer_token="fixture",journal=journal,
                budget=CompletionBudget(seconds=.2,attempt_seconds=.2))
            try:
                return await client.complete(OrderIntent("r1","customer","key","1"))
            finally:
                await client.aclose()
        before = time.monotonic()
        try:
            result = asyncio.run(run())
            assert time.monotonic()-before < 2
            assert result["result"]=="EXHAUSTED" and result["attempts"]==1
            assert journal.snapshot()["outcomes"][0]["error"]=="transport_error"
        finally:
            journal.close()


def test_lost_response_is_resolved_by_real_http_lookup_without_reposting(tmp_path):
    app, committed, posts = FastAPI(), {}, []
    @app.post("/orders")
    async def submit():
        posts.append(True)
        committed.update(order_id=1,customer="customer",idempotency_key="key",amount="1.00",result="confirmed")
        async def body():
            yield b'{'
            await asyncio.sleep(10)
        return StreamingResponse(body(),media_type="application/json")
    @app.get("/orders")
    async def lookup():
        return committed
    with serve(app) as origin:
        journal = RequestJournal.create(tmp_path/"requests.sqlite",run_id="lost-response",tenant_id="tenant-a",profile_digest="a"*64)
        async def run():
            client = BoundedOrderClient(origin,bearer_token="fixture",journal=journal,jitter=lambda a,b:0,
                budget=CompletionBudget(seconds=1,attempt_seconds=.1))
            try:
                return await client.complete(OrderIntent("r1","customer","key","1"))
            finally:
                await client.aclose()
        try:
            result = asyncio.run(run())
            assert result["result"]=="CONFIRMED" and result["attempts"]==2
            assert len(posts)==1
            assert [o["status"] for o in journal.snapshot()["outcomes"]]==[None,200]
        finally:
            journal.close()
