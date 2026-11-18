"""Read-only observation of the existing PostgreSQL order business schema."""


def observe_orders(journal, connect):
    """Read final results independently in one read-only, repeatable snapshot.

    ``connect`` is an operator-configured connection to the final database,
    ideally with a SELECT-only role; no DSN is accepted from the HTTP caller.
    The caller must stop traffic and finish the handoff before this read. A
    complete SELECT is not proof of remote writer fencing or quiescence.
    Errors propagate: a failed read must not become an empty complete result.
    """
    snapshot = journal.snapshot()
    keys = sorted({(r["customer"], r["idempotency_key"]) for r in snapshot["requests"]})
    rows = []
    with connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        for customer, key in keys:
            found = conn.execute(
                "SELECT id,customer,idempotency_key,amount FROM orders "
                "WHERE customer=%s AND idempotency_key=%s", (customer, key)).fetchall()
            result = conn.execute("SELECT result FROM idempotency_results WHERE customer=%s AND idempotency_key=%s",
                                  (customer, key)).fetchone()
            if not found and result:
                rows.append({"order_id": None, "customer": customer, "idempotency_key": key,
                             "amount": "0", "result": result[0], "work_items": 0, "done_items": 0})
            for oid, customer, key, value in found:
                total, done = conn.execute(
                    "SELECT count(*),count(*) FILTER (WHERE state='done') FROM work_items WHERE order_id=%s",
                    (oid,)).fetchone()
                rows.append({"order_id": oid, "customer": customer, "idempotency_key": key,
                             "amount": str(value), "result": result[0] if result else None,
                             "work_items": total, "done_items": done})
    return {"scope": snapshot["scope"], "complete": True, "orders": rows}
