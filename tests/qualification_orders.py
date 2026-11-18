"""Disposable TCP serving and observations for the shared order canary.

Application behavior lives in sova.qualification.order_app and is reused by
both this local fixture and the physical AWS/Linux VM qualification image.
"""

from contextlib import contextmanager
import socket
import threading
import time

import uvicorn

from sova.qualification.order_app import application as application, receipt as receipt
from sova.qualification.postgres_orders import observe_orders


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
