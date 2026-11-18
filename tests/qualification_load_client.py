"""Bounded, closed-loop HTTPS fixture load with no hidden retries or failures.

One durable intent and attempt before each transmission. Failed initial orders
are looked up/retried with the same key only after the parent ends the load.
Every HTTP attempt remains in SQLite and the fsynced JSONL trace.
"""

from collections import Counter
import asyncio
from datetime import UTC, datetime
import json
import math
import os
import random
from pathlib import Path
import ssl
import socket
import time

import httpx

from sova.qualification.http_orders import OrderHttpClient
from sova.qualification.bounded_orders import BoundedOrderClient, CompletionBudget
from sova.qualification.requests import OrderIntent, RequestJournal


def metrics(events):
    if not events:
        raise ValueError("A nonempty traffic trace is required")
    durations = sorted(e["elapsed_ms"] for e in events)
    if any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in durations):
        raise ValueError("Finite nonnegative request durations required")
    counts = Counter(str(e["status"]) if e["status"] is not None else "transport_error" for e in events)
    initial = [e for e in events if e["phase"] == "load"]
    return {"attempts": len(events), "status_counts": dict(sorted(counts.items())),
            "initial_attempts": len(initial),
            "initial_failures": sum(e["status"] != 200 for e in initial),
            "recovery_attempts": sum(e["phase"] in ("inline_retry", "recovery") for e in events),
            "phase_counts": dict(sorted(Counter(e["phase"] for e in events).items())),
            "elapsed_ms": {"p50": durations[math.ceil(len(durations) * .5) - 1],
                           "p95": durations[math.ceil(len(durations) * .95) - 1], "max": durations[-1]},
            "load_model": "single closed-loop client; 0.2s think time; fresh connection per attempt",
            "service_continuity": "NOT_QUALIFIED", "qualification_issued": False}


def main():
    config_dir, result_dir = Path("/config"), Path("/results")
    config = json.loads((config_dir / "client.json").read_text())
    journal = RequestJournal.create(result_dir / "client.sqlite", run_id=config["run_id"],
                                    tenant_id=config["tenant_id"], profile_digest=config["profile_digest"])
    transport = httpx.Client(verify=ssl.create_default_context(cafile=str(config_dir / "ca.pem")),
        timeout=1.0, trust_env=False, follow_redirects=False, limits=httpx.Limits(max_keepalive_connections=0))
    client = OrderHttpClient(config["origin"], bearer_token=(config_dir / "token").read_text().strip(),
                             journal=journal, client=transport)
    events, pending = [], []
    side = None
    def response_seen(response):
        nonlocal side
        side = response.headers.get("x-qualification-side")
    transport.event_hooks["response"] = [response_seen]

    def attempt(intent, kind, phase):
        nonlocal side
        side = None
        at, start = datetime.now(UTC), time.monotonic()
        status = client.submit(intent) if kind == "submit" else client.lookup(intent)
        event = {"request_id": intent.request_id, "kind": kind, "phase": phase,
                 "started_at": at.isoformat(), "finished_at": datetime.now(UTC).isoformat(),
                 "elapsed_ms": round((time.monotonic() - start) * 1000, 3), "status": status, "side": side}
        with (result_dir / "traffic.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        events.append(event)
        return status

    stop_received = False
    try:
        tls = {"verified_https": False, "wrong_ca_denied": False, "wrong_hostname_denied": False}
        for name, context, hostname in (
                ("verified_https", ssl.create_default_context(cafile=str(config_dir / "ca.pem")), "orders.fixture"),
                ("wrong_ca_denied", ssl.create_default_context(), "orders.fixture"),
                ("wrong_hostname_denied", ssl.create_default_context(cafile=str(config_dir / "ca.pem")), "wrong.fixture")):
            try:
                with socket.create_connection(("orders.fixture", 30443), timeout=2) as sock:
                    with context.wrap_socket(sock, server_hostname=hostname):
                        if name != "verified_https":
                            raise RuntimeError("Invalid server identity was accepted")
                        tls[name] = True
            except ssl.SSLCertVerificationError:
                if name == "verified_https":
                    raise
                tls[name] = True
        bounded_results = []
        async def run_load():
            nonlocal stop_received
            async def bounded_run(transport):
                nonlocal stop_received
                def observe(event):
                    event["phase"] = "load" if event["sequence"] == 1 else "inline_retry"
                    with (result_dir / "traffic.jsonl").open("a") as stream:
                        stream.write(json.dumps(event) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    events.append(event)
                bounded = BoundedOrderClient(config["origin"], bearer_token=(config_dir / "token").read_text().strip(),
                    journal=journal, client=transport, budget=CompletionBudget(), observer=observe,
                    jitter=random.Random(0).uniform)
                end, number = time.monotonic() + 120, 0
                while time.monotonic() < end:
                    minimum = config.get("minimum_orders", 0)
                    if (result_dir / "stop").exists() and number >= minimum:
                        stop_received = True
                        break
                    if minimum and number >= minimum:
                        await asyncio.sleep(.05)
                        continue
                    intent = OrderIntent(f"request-{number:05d}", "load-" + config["run_id"], f"key-{number:05d}", "12.34")
                    if config.get("mode") == "prepared_bounded":
                        outcome = await bounded.complete(intent)
                        bounded_results.append(outcome)
                        if outcome["result"] != "CONFIRMED":
                            pending.append(intent)
                    elif attempt(intent, "submit", "load") != 200:
                        pending.append(intent)
                    number += 1
                    await asyncio.sleep(.2)
            async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(config_dir / "ca.pem")),
                timeout=1, trust_env=False, follow_redirects=False, limits=httpx.Limits(max_keepalive_connections=0)) as async_transport:
                await bounded_run(async_transport)
        asyncio.run(run_load())
        # No transparent retry in the first-attempt trace. Recovery is explicit
        # and uses this same process/clock and unchanged business keys.
        for intent in pending:
            if attempt(intent, "lookup", "recovery") == 404:
                attempt(intent, "submit", "recovery")
        # Separate probes verify the live target even if the finite measured
        # load ended before handoff. Never relabel these as initial successes.
        if config.get("minimum_orders"):
            for number in range(3):
                intent = OrderIntent(f"probe-{number:05d}", "load-" + config["run_id"],
                                     f"probe-key-{number:05d}", "12.34")
                if attempt(intent, "submit", "post_handoff_probe") != 200 or side != "target":
                    raise RuntimeError("Post-handoff target probe failed")
        snapshot = journal.snapshot()
        (result_dir / "journal.json").write_text(json.dumps(snapshot, indent=2) + "\n")
        report = {"stop_received": stop_received, "tls": tls, "mode": config.get("mode", "cold_deferred"),
                  "bounded_results": bounded_results, **metrics(events)}
        (result_dir / "client.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        transport.close()
        journal.close()
        # Generated business identifiers only, inside the parent's private run
        # directory. Permit the host observer to reopen a container-owned file.
        (result_dir / "client.sqlite").chmod(0o666)
    return 0 if stop_received else 1


if __name__ == "__main__":
    raise SystemExit(main())
