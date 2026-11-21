#!/usr/bin/env python3
"""Regression tests for the two medium findings of the 7de1210 security audit.

1. The per-peer deadline must also abort a peer that dribbles HTTP response
   HEADERS — close() from the watchdog thread does not interrupt a blocked
   recv, so SHUT_RDWR is required.
2. The Claude hook status reader must enforce path integrity on the HANDLE it
   reads (O_NOFOLLOW|O_NONBLOCK, ownership, size), not with pre-open lstat
   checks that a same-user process can race.

Hermetic: a loopback socket and a scratch directory under $HOME only.

Run: python3 tests/test_audit_hardening.py
"""

import importlib.util
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load from source, with HOME pointed at a scratch dir before import so no
# module constant can bind to the real profile.
_saved_home = os.environ.get("HOME")
os.environ["HOME"] = tempfile.mkdtemp(prefix="audit-hardening-home-")
try:
    _spec = importlib.util.spec_from_file_location(
        "agent_ctl_audit", os.path.join(ROOT, "agent_ctl.py"))
    assert _spec and _spec.loader
    ac = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ac)
finally:
    if _saved_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _saved_home


class PeerHeaderDeadline(unittest.TestCase):
    def test_dribbled_headers_cannot_outlive_the_deadline(self):
        stop = threading.Event()
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.addCleanup(lambda: (stop.set(), listener.close()))

        def dribble():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                try:
                    request = b""
                    while b"\r\n\r\n" not in request:
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        request += chunk
                    # 0.4 s per byte: each recv resets the socket's 1 s timeout,
                    # so only a TOTAL deadline can bound this.
                    for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                        if stop.is_set():
                            return
                        conn.sendall(bytes([byte]))
                        time.sleep(0.4)
                except OSError:
                    return

        threading.Thread(target=dribble, daemon=True).start()
        observed = {}

        def call():
            observed["value"] = ac.peer_http_get(
                f"http://127.0.0.1:{port}", "/api/sessions", "k" * 8,
                deadline=time.monotonic() + 2.0)

        started = time.monotonic()
        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(6.0)
        self.assertFalse(worker.is_alive(),
                         "the per-peer deadline did not abort a header-phase dribble")
        self.assertLess(time.monotonic() - started, 4.5)
        self.assertIsNone(observed.get("value"))


class ClaudeHookStatusReader(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="audit-hook-status-",
                                    dir=os.path.expanduser("~"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._saved_dir = ac.CLAUDE_HOOK_STATUS_DIR
        ac.CLAUDE_HOOK_STATUS_DIR = self.dir
        self.addCleanup(setattr, ac, "CLAUDE_HOOK_STATUS_DIR", self._saved_dir)
        self.pid = os.getpid()
        self.path = os.path.join(self.dir, f"{self.pid}.json")
        self.payload = {
            "status": "waiting", "detail": "Needs your input",
            "cwd": "/home/user/project", "updated": int(time.time()),
        }

    def _write(self, payload):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.chmod(self.path, 0o600)

    def test_a_valid_owner_only_status_file_is_read(self):
        self._write(self.payload)
        self.assertEqual(ac.read_claude_hook_status(self.pid)["status"], "waiting")

    def test_a_symlinked_status_file_is_refused(self):
        real = os.path.join(self.dir, "real.json")
        with open(real, "w", encoding="utf-8") as fh:
            json.dump(self.payload, fh)
        os.chmod(real, 0o600)
        os.symlink(real, self.path)
        self.assertIsNone(ac.read_claude_hook_status(self.pid),
                          "a symlinked status file was followed")

    def test_a_fifo_status_file_cannot_wedge_the_collector(self):
        os.mkfifo(self.path)
        observed = []
        worker = threading.Thread(
            target=lambda: observed.append(ac.read_claude_hook_status(self.pid)),
            daemon=True)
        worker.start()
        worker.join(2.0)
        self.assertFalse(worker.is_alive(), "a planted FIFO wedged the reader")
        self.assertEqual(observed, [None])

    def test_a_future_timestamp_is_refused(self):
        future = dict(self.payload,
                      updated=int(time.time()) + 2 * ac.CLAUDE_HOOK_STATUS_MAX_AGE)
        self._write(future)
        self.assertIsNone(ac.read_claude_hook_status(self.pid),
                          "a future-dated status was accepted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
