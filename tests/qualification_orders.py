"""Loopback-only HTTP surface over the existing real PostgreSQL order fixture.

Not a customer API or an authentication product. The generated token protects
only this disposable run; no source fencing, route switching or approvals are
implemented here. Those stay in the existing handoff adapter/control plane.
"""

from contextlib import contextmanager
from decimal import Decimal
import hmac
import json
import socket
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import psycopg
import uvicorn

from sova.qualification.postgres_orders import observe_orders
from sova.qualification.requests import OrderIntent


def receipt(row):
    return {"order_id": row[0], "customer": row[1], "idempotency_key": row[2],
            "amount": str(row[3]), "result": row[4]}


def application(business, route, scope, token, *, after_commit=None, paused=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    select = ("SELECT o.id,o.customer,o.idempotency_key,o.amount,r.result FROM orders o "
              "JOIN idempotency_results r USING(customer,idempotency_key) "
              "WHERE o.customer=%s AND o.idempotency_key=%s")

    @app.middleware("http")
    async def authorize(request, call_next):
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if (request.headers.get("x-qualification-tenant") != scope["tenant_id"]
                or request.headers.get("x-qualification-run") != scope["run_id"]):
            return JSONResponse({"error": "scope mismatch"}, status_code=403)
        return await call_next(request)

    @app.post("/orders")
    async def submit(request: Request):
        body = await request.body()
        if len(body) > 4096:
            return JSONResponse({"error": "request too large"}, status_code=413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) != {"customer", "amount"}:
                raise ValueError()
            intent = OrderIntent("http-order", data["customer"], request.headers.get("idempotency-key"),
                                 data["amount"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid order"}, status_code=422)
        if paused and paused.is_set():
            return JSONResponse({"error": "temporarily unavailable"}, status_code=503,
                                headers={"retry-after": "1"})
        try:
            with business["connect_as"](route.get(), business["app"]) as conn:
                row = conn.execute(
                    "INSERT INTO orders VALUES (nextval('order_number_seq'),%s,%s,%s) "
                    "ON CONFLICT(customer,idempotency_key) DO NOTHING RETURNING id",
                    (intent.customer, intent.idempotency_key, intent.amount)).fetchone()
                if row:
                    conn.execute("INSERT INTO work_items VALUES (%s,%s,'done')", (row[0], row[0]))
                    conn.execute("INSERT INTO idempotency_results VALUES (%s,%s,'confirmed')",
                                 (intent.customer, intent.idempotency_key))
                result = conn.execute(select, (intent.customer, intent.idempotency_key)).fetchone()
                if result is None or result[3] != Decimal(intent.amount):
                    raise ValueError("inconsistent business key")
                response = receipt(result)
        except psycopg.Error:
            return JSONResponse({"error": "temporarily unavailable"}, status_code=503,
                                headers={"retry-after": "1"})
        except ValueError:
            return JSONResponse({"error": "business conflict"}, status_code=409)
        if after_commit:
            after_commit(intent)
        return response

    @app.get("/orders")
    def lookup(customer: str, idempotency_key: str):
        try:
            OrderIntent("http-lookup", customer, idempotency_key, "1")
        except ValueError:
            return JSONResponse({"error": "invalid key"}, status_code=422)
        try:
            with business["connect_as"](route.get(), business["app"]) as conn:
                row = conn.execute(select, (customer, idempotency_key)).fetchone()
                return receipt(row) if row else JSONResponse({"error": "not found"}, status_code=404)
        except psycopg.Error:
            return JSONResponse({"error": "temporarily unavailable"}, status_code=503)

    return app


@contextmanager
def serve(app):
    """A real TCP listener on an OS-assigned loopback port, not TestClient."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("Loopback qualification server did not start")
            time.sleep(0.01)
        yield "http://127.0.0.1:" + str(sock.getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Loopback qualification server did not stop")


def observe(business, database, journal):
    return observe_orders(journal, lambda: business["connect"](database))
