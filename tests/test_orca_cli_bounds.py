#!/usr/bin/env python3
"""Bounds and lifetime guards for the Orca CLI invocation in agent_ctl.py.

Every fixture here is a local stub spawned through query_orca_terminals():
no orca binary, no runtime socket, no network. Safe to run on a live desktop
with real agents and a real Orca runtime attached.

The regression these tests pin down (marketplace review block on [Verify]
#14838): the old code did a blocking proc.stdout.read() BEFORE proc.wait
(timeout=...), so a runtime that held the pipe open without writing parked
the collector forever — and the panel then killed only the Python collector,
leaving the CLI (and helpers it forked) alive to accumulate across retries.

Run:  python3 tests/test_orca_cli_bounds.py
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_agent_ctl():
    spec = importlib.util.spec_from_file_location(
        "agent_ctl_under_test", os.path.join(REPO_ROOT, "agent_ctl.py")
    )
    assert spec is not None and spec.loader is not None, "cannot load agent_ctl.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ac = _load_agent_ctl()


def _proc_state(pid):
    """Process state letter from /proc/<pid>/stat, or None if it is gone.

    comm sits in parentheses and may contain spaces, so slice after the LAST
    closing paren rather than splitting the line.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    end = data.rfind(")")
    return data[end + 2:end + 3] or None


def _alive(pid):
    """True while the process exists AND has not become a zombie.

    A killed orphan is reparented and may sit as a zombie where a subreaper
    exists; counting that as alive would make "was it killed?" assertions pass
    when the guard they test is broken.
    """
    state = _proc_state(pid)
    return state is not None and state != "Z"


def _wait_gone(pid, seconds=3.0):
    """True once the pid is gone or a zombie (pid 0 is never a real target)."""
    if pid <= 0:
        return False
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return False


class OrcaCliBoundsCase(unittest.TestCase):
    """Each test points ORCA_CLI at a purpose-built stub."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.pidfile = os.path.join(self.dir, "stub.pid")
        # Absolute cache timestamps assume machine uptime, so derive them, and
        # every test gets its own tick so a latched failure cannot leak across.
        stale = time.monotonic() - ac._ORCA_CACHE_TTL - 1.0
        ac._ORCA_CACHE.update({
            "ts": stale, "failed_ts": stale, "terminals": [],
            "failed_tick": -1, "tick": ac._ORCA_CACHE["tick"] + 1,
        })

    def tearDown(self):
        # Sweep the pidfile AND any forked kid: an assertion that fails before
        # a kid's cleanup is registered must not leave it sleeping for 120s.
        pids = self._all_pids()
        try:
            with open(os.path.join(self.dir, "kid.pid")) as f:
                pids.append(int(f.read().strip()))
        except (OSError, ValueError):
            pass
        for pid in pids:
            if pid > 0 and _alive(pid):
                try:
                    os.kill(pid, 9)
                except (ProcessLookupError, PermissionError):
                    pass

    def _stub(self, body):
        """Write an executable stub that records its pid, then run `body`."""
        path = os.path.join(self.dir, "stub.py")
        with open(path, "w") as f:
            f.write(f"#!{sys.executable}\n")
            f.write(textwrap.dedent(f"""
                import os, signal, sys, time
                with open({self.pidfile!r}, "a") as fh:
                    fh.write(str(os.getpid()) + "\\n")
            """))
            f.write(textwrap.dedent(body))
        os.chmod(path, 0o755)
        ac.ORCA_CLI = path
        return path

    def _all_pids(self):
        try:
            with open(self.pidfile) as f:
                return [int(line) for line in f.read().split() if line.strip()]
        except (OSError, ValueError):
            return []

    def _stub_pid(self):
        pids = self._all_pids()
        return pids[-1] if pids else 0

    def _spawn_count(self):
        return len(self._all_pids())

    # --- guards -------------------------------------------------------------

    def test_stalled_cli_cannot_park_the_collector(self):
        """Holds stdout open, writes nothing: the deadline must still expire."""
        self._stub("time.sleep(120)\n")
        t0 = time.monotonic()
        rows = ac.query_orca_terminals(timeout=0.5)
        elapsed = time.monotonic() - t0
        pid = self._stub_pid()
        self.assertTrue(pid, "the stub never ran, so nothing was exercised")
        self.assertEqual(rows, [])
        self.assertLess(elapsed, 3.0, "blocking read outlived its deadline again")
        self.assertGreaterEqual(elapsed, 0.4, "deadline fired before the budget")
        self.assertTrue(_wait_gone(pid), "stalled CLI survived the timeout")

    def test_stalled_cli_ignoring_sigterm_is_sigkilled(self):
        """The two-stage group kill must escalate past a SIGTERM-ignoring CLI."""
        self._stub("""
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(120)
        """)
        t0 = time.monotonic()
        ac.query_orca_terminals(timeout=0.4)
        elapsed = time.monotonic() - t0
        pid = self._stub_pid()
        self.assertTrue(pid, "the stub never ran, so nothing was exercised")
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertLess(elapsed, 3.0, "SIGTERM escalation did not reach SIGKILL")
        self.assertTrue(_wait_gone(pid), "SIGTERM-ignoring CLI survived")

    def test_runaway_output_is_cut_at_the_cap(self):
        """A producer that never stops writing is capped fast, not buffered.

        The stub paces itself so that dropping the cap shows up as elapsed
        time (~4 s, the whole budget) instead of as unbounded memory.
        """
        self._stub("""
            chunk = b"x" * 8192
            while True:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                time.sleep(0.01)
        """)
        t0 = time.monotonic()
        rows = ac.query_orca_terminals(timeout=4.0)
        elapsed = time.monotonic() - t0
        pid = self._stub_pid()
        self.assertTrue(pid, "the stub never ran, so nothing was exercised")
        self.assertEqual(rows, [])
        self.assertLess(elapsed, 1.0, "the byte cap never fired; the budget did")
        self.assertTrue(_wait_gone(pid), "runaway CLI survived the cap")

    def test_last_known_good_survives_a_stall(self):
        """A wedged tick must serve the previous roster, not blank it."""
        good = [{"handle": "keep-me", "title": "Hermes"}]
        stale = time.monotonic() - ac._ORCA_CACHE_TTL - 1.0
        ac._ORCA_CACHE.update({"ts": stale, "failed_ts": stale, "terminals": good})
        self._stub("time.sleep(120)\n")
        t0 = time.monotonic()
        self.assertEqual(ac.query_orca_terminals(timeout=0.4), good)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(ac._ORCA_CACHE["ts"], stale, "failure refreshed the success timestamp")

    def test_failure_backoff_skips_the_second_probe_in_one_tick(self):
        """A tick probes the CLI twice; one failure must not cost two budgets."""
        self._stub("time.sleep(120)\n")
        ac.query_orca_terminals(timeout=0.4)
        self.assertEqual(self._spawn_count(), 1)
        t0 = time.monotonic()
        ac.query_orca_terminals(timeout=4.0)
        self.assertLess(time.monotonic() - t0, 0.1, "second probe re-paid the budget")
        self.assertEqual(self._spawn_count(), 1, "the CLI was spawned twice in one tick")

    def test_failed_probe_is_not_retried_within_the_same_tick(self):
        """The tick latch must hold even when the wall-clock back-off expires.

        The two probes in a tick are separated by the peer stage, so a degraded
        tick can outrun _ORCA_CACHE_TTL; the tick identity, not elapsed time,
        is what forbids paying twice.
        """
        self._stub("time.sleep(120)\n")
        ac.query_orca_terminals(timeout=0.4)
        self.assertEqual(self._spawn_count(), 1)
        ac._ORCA_CACHE["failed_ts"] = time.monotonic() - ac._ORCA_CACHE_TTL - 1.0
        t0 = time.monotonic()
        ac.query_orca_terminals(timeout=4.0)
        self.assertLess(time.monotonic() - t0, 0.1, "same tick re-paid the budget")
        self.assertEqual(self._spawn_count(), 1, "same tick probed the CLI twice")

    def test_new_tick_releases_the_failure_latch(self):
        """The latch must release: a later tick probes the runtime again."""
        self._stub("time.sleep(120)\n")
        ac.query_orca_terminals(timeout=0.3)
        self.assertEqual(self._spawn_count(), 1)
        ac._ORCA_CACHE["tick"] += 1
        ac._ORCA_CACHE["failed_ts"] = time.monotonic() - ac._ORCA_CACHE_TTL - 1.0
        ac.query_orca_terminals(timeout=0.3)
        self.assertEqual(self._spawn_count(), 2, "a new tick never re-probed the CLI")

    def test_status_build_bumps_the_orca_tick(self):
        """fetch_all_agents is what advances the tick the latch is scoped to."""
        before = ac._ORCA_CACHE["tick"]
        with mock.patch.object(ac, "get_herdr_server_pids", return_value=[]), \
             mock.patch.object(ac, "herdr_session_sockets", return_value=[]), \
             mock.patch.object(ac, "herdr_core_sockets", return_value=[]), \
             mock.patch.object(ac, "herdr_machine_list", return_value=[]), \
             mock.patch.object(ac, "scan_orca_agents", return_value=[]), \
             mock.patch.object(ac, "scan_standalone_agents", return_value=[]), \
             mock.patch.object(ac, "_collect_peer_cards", return_value=([], [])), \
             mock.patch.object(ac, "query_orca_terminals", return_value=[]):
            ac.fetch_all_agents()
        self.assertEqual(ac._ORCA_CACHE["tick"], before + 1)

    def test_grandchildren_are_killed_with_the_group(self):
        """A CLI helper that ignores SIGTERM must not outlive the retry."""
        kidfile = os.path.join(self.dir, "kid.pid")
        self._stub(f"""
            kid = os.fork()
            if kid == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                open({kidfile!r}, "w").write(str(os.getpid()))
                time.sleep(120)
                os._exit(0)
            while not os.path.exists({kidfile!r}):
                time.sleep(0.01)
            time.sleep(120)
        """)
        t0 = time.monotonic()
        ac.query_orca_terminals(timeout=0.4)
        self.assertLess(time.monotonic() - t0, 3.0)
        with open(kidfile) as f:
            kid = int(f.read().strip())
        self.addCleanup(lambda: os.path.exists(f"/proc/{kid}") and os.kill(kid, 9))
        self.assertTrue(_wait_gone(self._stub_pid()), "CLI leader survived")
        self.assertTrue(_wait_gone(kid), "CLI grandchild survived the group kill")

    def test_leader_exit_leaves_no_helper_behind(self):
        """The CLI may exit while a helper keeps stdout open: kill it anyway.

        Group cleanup must not be gated on the leader still being alive — the
        helper inherits fd 1, so the drain times out with the leader already
        gone, and PDEATHSIG does not reach fork()ed children.
        """
        kidfile = os.path.join(self.dir, "kid.pid")
        self._stub(f"""
            kid = os.fork()
            if kid == 0:
                open({kidfile!r}, "w").write(str(os.getpid()))
                time.sleep(120)
                os._exit(0)
            while not os.path.exists({kidfile!r}):
                time.sleep(0.01)
            os._exit(0)   # leader exits, helper holds stdout open
        """)
        ac.query_orca_terminals(timeout=0.4)
        with open(kidfile) as f:
            kid = int(f.read().strip())
        self.addCleanup(lambda: os.path.exists(f"/proc/{kid}") and os.kill(kid, 9))
        self.assertTrue(_wait_gone(kid), "helper outlived a leader that exited first")

    # --- happy path must stay intact ---------------------------------------

    def test_nonzero_exit_kills_a_forked_helper(self):
        """A non-zero exit is a failed call: its helpers are reaped too."""
        kidfile = os.path.join(self.dir, "kid.pid")
        self._stub(f"""
            kid = os.fork()
            if kid == 0:
                open({kidfile!r}, "w").write(str(os.getpid()))
                os.dup2(os.open(os.devnull, os.O_WRONLY), 1)  # release the pipe
                time.sleep(120)
                os._exit(0)
            while not os.path.exists({kidfile!r}):
                time.sleep(0.01)
            sys.exit(1)
        """)
        rows = ac.query_orca_terminals(timeout=4.0)
        with open(kidfile) as f:
            kid = int(f.read().strip())
        self.addCleanup(lambda: _alive(kid) and os.kill(kid, 9))
        self.assertEqual(rows, [])
        self.assertTrue(_wait_gone(kid), "a non-zero exit left its helper behind")

    def test_clean_poll_leaves_the_cli_session_alone(self):
        """A successful call must signal nothing: leftovers are the runtime's.

        The CLI is a session leader here, so a daemon it auto-started (without
        detaching) shares its group. Killing that on every 5s poll would drop
        live Orca terminals, so the group kill is reserved for failed calls.
        """
        kidfile = os.path.join(self.dir, "kid.pid")
        payload = '{"ok": true, "result": {"terminals": []}}'
        self._stub(f"""
            kid = os.fork()
            if kid == 0:
                open({kidfile!r}, "w").write(str(os.getpid()))
                os.dup2(os.open(os.devnull, os.O_WRONLY), 1)  # release the pipe
                time.sleep(120)
                os._exit(0)
            while not os.path.exists({kidfile!r}):
                time.sleep(0.01)
            sys.stdout.write({payload!r})
            sys.stdout.flush()
            sys.exit(0)
        """)
        rows = ac.query_orca_terminals(timeout=4.0)
        with open(kidfile) as f:
            kid = int(f.read().strip())
        self.addCleanup(lambda: os.path.exists(f"/proc/{kid}") and os.kill(kid, 9))
        self.assertEqual(rows, [])
        # Signal delivery is asynchronous: give a kill time to land, or this
        # assertion would pass on a racy implementation that kills afterwards.
        time.sleep(0.3)
        self.assertTrue(_alive(kid),
                        "a clean poll killed a process the runtime left behind")

    def test_valid_payload_still_parses_and_caches(self):
        payload = '{"ok": true, "result": {"terminals": [{"title": "Hermes", "handle": "h1"}]}}'
        self._stub(f"sys.stdout.write({payload!r})\nsys.stdout.flush()\n")
        rows = ac.query_orca_terminals(timeout=4.0)
        self.assertEqual(rows, [{"title": "Hermes", "handle": "h1"}])
        self.assertGreater(ac._ORCA_CACHE["ts"], 0.0)
        self.assertEqual(ac._ORCA_CACHE["terminals"], rows)

    def test_nonzero_exit_serves_last_known_good(self):
        good = [{"handle": "keep-me"}]
        stale = time.monotonic() - ac._ORCA_CACHE_TTL - 1.0
        ac._ORCA_CACHE.update({"ts": stale, "failed_ts": stale, "terminals": good})
        self._stub("sys.stdout.write('{\"ok\": true}')\nsys.exit(1)\n")
        self.assertEqual(ac.query_orca_terminals(timeout=4.0), good)
        self.assertEqual(ac._ORCA_CACHE["ts"], stale, "failure refreshed the success timestamp")
        self.assertEqual(ac._ORCA_CACHE["terminals"], good, "failure poisoned the roster")
        self.assertGreater(ac._ORCA_CACHE["failed_ts"], stale, "failure was not recorded")

    # --- lifetime guard: the CLI dies with the collector -------------------

    def test_cli_dies_when_collector_is_sigkilled(self):
        """PR_SET_PDEATHSIG: the panel's SIGKILL of the collector must reach it.

        Runs the real function inside a throwaway collector process, then
        SIGKILLs that process mid-call — the exact path no Python cleanup can
        cover — and requires the CLI to die with it.
        """
        log = os.path.join(self.dir, "collector.log")
        self._stub("time.sleep(120)\n")
        collector = textwrap.dedent(f"""
            import importlib.util
            spec = importlib.util.spec_from_file_location("ac", {os.path.join(REPO_ROOT, 'agent_ctl.py')!r})
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            m.ORCA_CLI = {os.path.join(self.dir, 'stub.py')!r}
            open({log!r}, "w").write("ready")
            m.query_orca_terminals(timeout=30)
        """)
        proc = subprocess.Popen([sys.executable, "-c", collector])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if os.path.exists(self.pidfile) and os.path.exists(log):
                break
            time.sleep(0.02)
        else:
            self.fail("collector never spawned the stub")
        pid = self._stub_pid()
        self.assertTrue(pid, "stub did not record its pid")
        proc.kill()
        proc.wait(timeout=10)
        self.assertTrue(_wait_gone(pid), "CLI outlived its SIGKILLed collector")


if __name__ == "__main__":
    unittest.main(verbosity=2)
