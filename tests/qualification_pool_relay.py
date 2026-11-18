"""Opaque loopback TCP relay: verified TLS remains end-to-end to the real PG.

Qualification fault injection only. Never parse or store TLS/SQL payloads.
"""

import ipaddress
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import threading
import time

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo


class Relay:
    def __init__(self, dsn):
        config = conninfo_to_dict(dsn)
        host = config.get('hostaddr', config.get('host', ''))
        if not ipaddress.ip_address(host).is_loopback or config.get('sslmode') != 'verify-full':
            raise ValueError('Only a certificate-verified loopback test DB may be relayed')
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            if conn.execute("SELECT current_setting('cluster_name'),current_database()").fetchone() != (
                    'sova-persistence-test', 'sova_test'):
                raise ValueError('Relay requires the explicitly isolated qualification database')
        self.upstream = (host, int(config['port']))
        self.pause, self.blocked, self.stopped = threading.Event(), threading.Event(), threading.Event()
        self.sockets, self.threads = [], []
        self.held_bytes = 0
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(4)
        self.listener.settimeout(.05)
        self.dsn = make_conninfo(dsn, port=self.listener.getsockname()[1])

    def pump(self, source, target, server_direction):
        try:
            while not self.stopped.is_set():
                try:
                    data = source.recv(16384)
                except socket.timeout:
                    continue
                if not data:
                    break
                if server_direction and self.pause.is_set():
                    self.held_bytes += len(data)
                    self.blocked.set()
                    while self.pause.is_set() and not self.stopped.wait(.01):
                        pass
                if self.stopped.is_set():
                    break
                target.sendall(data)
        except OSError:
            pass  # Teardown closes run-owned sockets; no reconnect or replay.

    def accept(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                server = socket.create_connection(self.upstream, timeout=1)
            except OSError:
                client.close()
                self.stopped.set()
                break
            self.sockets.extend([client, server])
            for source, target, direction in [(client, server, False), (server, client, True)]:
                source.settimeout(.05)
                thread = threading.Thread(target=self.pump, args=(source, target, direction), daemon=True)
                self.threads.append(thread)
                thread.start()

    def __enter__(self):
        thread = threading.Thread(target=self.accept, daemon=True)
        self.threads.append(thread)
        thread.start()
        return self

    def __exit__(self, *exc):
        self.stopped.set()
        self.pause.clear()
        self.listener.close()
        for sock in self.sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        for thread in self.threads:
            thread.join(timeout=1)
        if any(t.is_alive() for t in self.threads):
            raise RuntimeError('Run-owned relay thread did not stop')


class Child:
    def __init__(self, dsn, role, stage):
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('qualification_pool_child.py'))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b''
        self.messages = []
        self.forced_stop = False
        # Credentials are private stdin, never command arguments or output.
        self.send({'dsn': dsn, 'role': role, 'stage': stage})

    def send(self, value):
        self.process.stdin.write(json.dumps(value).encode()+b'\n')
        self.process.stdin.flush()

    def receive(self, timeout):
        deadline = time.monotonic()+timeout
        while b'\n' not in self.buffer:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not self.selector.select(timeout=remaining):
                return None
            data = os.read(self.process.stdout.fileno(), 4096)
            if not data:
                raise RuntimeError('Qualification child exited without expected message')
            self.buffer += data
            if len(self.buffer) > 16384:
                raise RuntimeError('Qualification child output exceeded bound')
        line, self.buffer = self.buffer.split(b'\n', 1)
        message = json.loads(line)
        self.messages.append(message)
        return message

    def close(self):
        if self.process.poll() is None:
            self.forced_stop = True
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.selector.close()
        self.process.stdin.close()
        self.process.stdout.close()
