"""A loopback relay that keeps ``kubectl port-forward`` alive across resets.

``kubectl port-forward`` ends ("lost connection to pod") when the Pod resets
one forwarded connection - which a TLS-only port does to a plaintext attempt
(measured with Valkey and kubectl 1.36). A check that brackets such an
attempt between two connections needs the second one to arrive. The relay
listens on a fixed loopback port; each connection it accepts is piped
through a running port-forward, started again first if the last one ended.
"""

import re
import socket
import subprocess
import threading


class ForwardRelay:
    def __init__(self, context, namespace, target, port):
        from kind_support import _config

        self.command = ["kubectl", *(["--kubeconfig", c] if (c := _config(context)) else []),
                        "--context", context, "-n", namespace, "port-forward", target,
                        f":{port}"]
        self.process, self.forwarded, self.lock = None, None, threading.Lock()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _forward(self):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.process = subprocess.Popen(self.command, stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL, text=True)
                found = re.search(r"127\.0\.0\.1:(\d+)", self.process.stdout.readline())
                if not found:
                    self.process.kill()
                    raise OSError("port-forward did not start")
                self.forwarded = int(found.group(1))
            return self.forwarded

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._relay, args=(client,), daemon=True).start()

    def _relay(self, client):
        try:
            upstream = socket.create_connection(("127.0.0.1", self._forward()), timeout=10)
        except OSError:
            client.close()
            return

        def pipe(source, sink):
            try:
                while chunk := source.recv(65536):
                    sink.sendall(chunk)
            except OSError:
                pass
            finally:
                for end in (source, sink):
                    try:
                        end.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
        pipe(client, upstream)

    def close(self):
        self.listener.close()
        if self.process is not None:
            self.process.terminate()
