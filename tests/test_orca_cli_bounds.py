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
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import types
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_agent_ctl():
    """Load agent_ctl.py from SOURCE, never from a bytecode cache.

    The mutation sweep rewrites this file between runs, and two mutations can
    produce the same file size: a pyc whose mtime (whole seconds) and size both
    match would then hide the edit and report a guard as SURVIVED when it was
    actually removed. Compiling the text we just read cannot go stale.
    """
    path = os.path.join(REPO_ROOT, "agent_ctl.py")
    module = types.ModuleType("agent_ctl_under_test")
    module.__file__ = path
    with open(path, encoding="utf-8") as f:
        code = compile(f.read(), path, "exec")
    exec(code, module.__dict__)
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
        # Every case is hermetic about the journal: nothing may land in the
        # real $XDG_RUNTIME_DIR while the suite runs.
        self._saved_journal_dir = ac._ORCA_JOURNAL_DIR
        ac._ORCA_JOURNAL_DIR = self.dir

    @property
    def journal(self):
        return ac._orca_journal_path()

    def tearDown(self):
        ac._ORCA_JOURNAL_DIR = self._saved_journal_dir
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


class OrcaTickDeadlineCase(OrcaCliBoundsCase):
    """The status tick must never hand the panel watchdog an in-flight probe.

    The panel kills the collector at Panel.qml's fetchStallTimer interval, and
    a kill lands after Python stops running: the in-band group cleanup is
    skipped, and PDEATHSIG does not reach the CLI's forked helpers. So one
    wall-clock deadline is published for the whole tick and the Orca probe is
    clamped to what is left of it — a probe that cannot finish its cleanup is
    never started at all.
    """

    def setUp(self):
        super().setUp()
        self._saved_deadline = ac._TICK_DEADLINE
        ac._TICK_DEADLINE = None

    def tearDown(self):
        ac._TICK_DEADLINE = self._saved_deadline
        super().tearDown()

    def test_remaining_tick_budget_caps_the_call(self):
        """A long call budget must be clamped to what the tick has left."""
        ac._TICK_DEADLINE = time.monotonic() + 3.0
        self._stub("time.sleep(120)\n")
        t0 = time.monotonic()
        rows = ac.query_orca_terminals(timeout=30.0)
        elapsed = time.monotonic() - t0
        self.assertEqual(rows, [])
        self.assertLess(elapsed, 6.0, "the 30 s call budget was not clamped to the tick")
        self.assertGreaterEqual(elapsed, 2.0, "the tick budget expired before it was spent")
        self.assertTrue(_wait_gone(self._stub_pid()), "the clamped call left the CLI alive")

    def test_probe_is_not_started_without_room_to_clean_up(self):
        """Below the floor: no spawn, no helper to orphan, last-known-good."""
        good = [{"handle": "keep-me"}]
        stale = time.monotonic() - ac._ORCA_CACHE_TTL - 1.0
        ac._ORCA_CACHE.update({"ts": stale, "failed_ts": stale, "terminals": good})
        ac._TICK_DEADLINE = time.monotonic() + ac._ORCA_MIN_BUDGET + ac._ORCA_CLEANUP_RESERVE - 0.2
        self._stub("time.sleep(120)\n")
        rows = ac.query_orca_terminals(timeout=4.0)
        self.assertEqual(rows, good, "a skipped probe must serve last-known-good")
        self.assertEqual(self._spawn_count(), 0,
                         "a probe started that could not finish its cleanup in-band")
        self.assertGreater(ac._ORCA_CACHE["failed_ts"], stale, "the skip was not recorded")

    def test_a_slow_success_is_not_re_probed_in_the_same_tick(self):
        """The success stamp is taken at completion, not at entry.

        A probe can take longer than _ORCA_CACHE_TTL and still succeed; if the
        stamp were taken before the call, the second probe in the same tick
        would spawn the CLI again and pay the budget twice.
        """
        original_ttl = ac._ORCA_CACHE_TTL
        self.addCleanup(setattr, ac, "_ORCA_CACHE_TTL", original_ttl)
        ac._ORCA_CACHE_TTL = 0.4
        payload = '{"ok": true, "result": {"terminals": []}}'
        self._stub(f"""
            time.sleep(1.0)
            sys.stdout.write({payload!r})
            sys.stdout.flush()
        """)
        self.assertEqual(ac.query_orca_terminals(timeout=4.0), [])
        self.assertEqual(self._spawn_count(), 1)
        t0 = time.monotonic()
        ac.query_orca_terminals(timeout=4.0)
        self.assertLess(time.monotonic() - t0, 0.2, "the second probe re-paid the budget")
        self.assertEqual(self._spawn_count(), 1, "a slow success was probed twice in one tick")

    def test_the_teardown_reserve_is_held_back_from_the_probe(self):
        """Time for the group kill comes out of the probe's own budget."""
        ac._TICK_DEADLINE = (time.monotonic() + ac._ORCA_MIN_BUDGET
                             + ac._ORCA_CLEANUP_RESERVE + 0.25)
        self._stub("time.sleep(120)\n")
        t0 = time.monotonic()
        ac.query_orca_terminals(timeout=30.0)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.5, "the probe did not get its slice")
        self.assertLess(elapsed, 1.2, "the teardown reserve was spent on the probe")
        self.assertTrue(_wait_gone(self._stub_pid()), "the probe's cleanup never ran")

    def test_budget_outside_a_tick_is_the_callers_own(self):
        ac._TICK_DEADLINE = None
        self.assertEqual(ac._tick_budget(4.0), 4.0)

    def test_spawn_overrun_is_cleaned_up_in_band(self):
        """A spawn that eats the budget must not leave a call for the watchdog.

        The admission check runs before Popen; a paused scheduler or a slow exec
        can cross the deadline during it, so the deadline is re-checked as soon
        as the pid is known.
        """
        marker = os.path.join(self.dir, "drain-ran")
        ac._TICK_DEADLINE = (time.monotonic() + ac._ORCA_MIN_BUDGET
                             + ac._ORCA_CLEANUP_RESERVE + 0.02)
        self._stub("time.sleep(120)\n")
        real_popen = subprocess.Popen

        def slow_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            time.sleep(0.8)  # the spawn itself overran the tick deadline
            return proc

        def fake_drain(*args, **kwargs):
            open(marker, "w").close()
            return b"", "eof"

        with mock.patch.object(ac.subprocess, "Popen", side_effect=slow_popen), \
             mock.patch.object(ac, "_drain_orca_stdout", side_effect=fake_drain):
            rows = ac.query_orca_terminals(timeout=30.0)
        self.assertEqual(rows, [])
        self.assertFalse(os.path.exists(marker), "the drain ran past the deadline")
        self.assertTrue(_wait_gone(self._stub_pid()), "the overrun call was not cleaned up")
        self.assertFalse(os.path.exists(self.journal), "the overrun call kept its record")

    def test_status_build_publishes_the_tick_deadline(self):
        """The deadline is set by fetch_all_agents, not derived per probe."""
        t0 = time.monotonic()
        with mock.patch.object(ac, "get_herdr_server_pids", return_value=[]), \
             mock.patch.object(ac, "herdr_session_sockets", return_value=[]), \
             mock.patch.object(ac, "herdr_core_sockets", return_value=[]), \
             mock.patch.object(ac, "herdr_machine_list", return_value=[]), \
             mock.patch.object(ac, "scan_orca_agents", return_value=[]), \
             mock.patch.object(ac, "scan_standalone_agents", return_value=[]), \
             mock.patch.object(ac, "_collect_peer_cards", return_value=([], [])), \
             mock.patch.object(ac, "query_orca_terminals", return_value=[]):
            ac.fetch_all_agents()
        self.assertIsNotNone(ac._TICK_DEADLINE, "a status build published no tick deadline")
        self.assertGreaterEqual(ac._TICK_DEADLINE, t0 + ac._TICK_WALL_BUDGET - 0.5)
        self.assertLessEqual(ac._TICK_DEADLINE, t0 + ac._TICK_WALL_BUDGET + 0.5)

    def test_panel_watchdog_outlasts_the_tick_budget(self):
        """Panel.qml's literal interval and the collector's budget must agree.

        Drift here is the block itself: a watchdog that fires before the tick
        budget expires kills a collector that was still going to clean up.
        """
        with open(os.path.join(REPO_ROOT, "Panel.qml"), encoding="utf-8") as f:
            qml = f.read()
        start = qml.index("id: fetchStallTimer")
        match = re.search(r"interval:\s*(\d+)", qml[start:start + 500])
        self.assertIsNotNone(match, "the stall timer lost its literal interval")
        interval = int(match.group(1)) / 1000.0
        self.assertAlmostEqual(
            interval, ac.PANEL_STALL_SECONDS, places=3,
            msg="Panel.qml and agent_ctl.py disagree about the watchdog interval",
        )
        self.assertGreaterEqual(
            interval - ac._TICK_WALL_BUDGET,
            ac._ORCA_CLEANUP_RESERVE + ac._ORCA_KILL_GRACE,
            "the margin cannot cover a teardown plus an orphan reap",
        )


class OrcaOrphanJournalCase(OrcaCliBoundsCase):
    """One record per collector; a dead collector's group is reaped."""

    def setUp(self):
        super().setUp()
        self._saved_deadline = ac._TICK_DEADLINE
        ac._TICK_DEADLINE = None

    def tearDown(self):
        ac._TICK_DEADLINE = self._saved_deadline
        super().tearDown()

    # --- fixtures ---------------------------------------------------------

    def _sleeping_group(self, ignore_sigterm=False):
        """A process in its own group that has ALREADY installed its handlers.

        Readiness matters: signalling before `signal.signal(...)` runs would
        kill the child by the default disposition and make the SIGTERM-ignoring
        cases pass without ever exercising the escalation.
        """
        ready = os.path.join(self.dir, f"ready-{time.monotonic_ns()}")
        body = "import time\n"
        if ignore_sigterm:
            body = ("import signal,time\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n")
        proc = subprocess.Popen(
            [sys.executable, "-c",
             body + f"open({ready!r}, 'w').close()\n" + "time.sleep(120)"],
            start_new_session=True,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 5.0
        while not os.path.exists(ready) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(os.path.exists(ready), "the stub never installed its handlers")
        return proc

    def _dead_owner(self):
        """A pid that has already exited, plus the start time that identifies it."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.15)"])
        start = ac._proc_starttime(proc.pid)
        proc.wait()
        self.assertIsNotNone(start, "could not read the owner's start time")
        return proc.pid, start

    def _dead_collector_with_group(self, ignore_sigterm=False):
        owner_pid, owner_start = self._dead_owner()
        group = self._sleeping_group(ignore_sigterm=ignore_sigterm)
        return owner_pid, owner_start, group

    def _write_record(self, owner_pid, owner_start, pgid, leader_start,
                      boot_id=None, uptime_ms=None):
        path = ac._orca_journal_path(owner_pid)
        self.assertIsNotNone(path, "journaling is disabled for a safe runtime dir")
        with open(path, "w") as f:
            f.write(f"{boot_id or ac._boot_id()} "
                    f"{ac._uptime_ms() if uptime_ms is None else uptime_ms} "
                    f"{pgid} {leader_start} {owner_start}\n")
        return path

    def _record_files(self):
        return [n for n in os.listdir(self.dir) if ac._orca_record_owner(n) is not None]

    # --- record format ----------------------------------------------------

    def test_record_is_owner_only_and_reads_back(self):
        proc = self._sleeping_group()
        ac._orca_record_orphan(proc.pid)
        path = self.journal
        self.assertIsNotNone(path, "journaling was disabled for a safe runtime dir")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        record = ac._orca_read_orphan()
        self.assertEqual(record[2], proc.pid)
        self.assertEqual(record[3], ac._proc_starttime(proc.pid))
        self.assertEqual(record[4], ac._proc_starttime(os.getpid()))
        self.assertEqual(record[0], ac._boot_id())

    def test_record_is_skipped_when_the_leader_is_already_gone(self):
        ac._orca_record_orphan(2 ** 22 + 12345)
        self.assertFalse(os.path.exists(self.journal))

    def test_failed_record_write_leaves_the_previous_record_intact(self):
        """The record is replaced atomically: a broken write cannot blank it.

        An empty or partial record reads exactly like "no orphan", so a kill
        between truncate and write would strand the helper this journal exists
        to reap. The new record is written beside the old one and renamed.
        """
        proc = self._sleeping_group()
        ac._orca_record_orphan(proc.pid)
        before = ac._orca_read_orphan()
        self.assertEqual(before[2], proc.pid)
        other = self._sleeping_group()
        with mock.patch.object(ac.os, "write", side_effect=OSError("ENOSPC")):
            ac._orca_record_orphan(other.pid)
        after = ac._orca_read_orphan()
        self.assertIsNotNone(after, "a failed write destroyed the previous record")
        self.assertEqual(after[2], proc.pid, "a failed write replaced the record")
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [],
                         "a failed write left a temp file behind")

    # --- reaping ----------------------------------------------------------

    def test_record_with_a_live_group_is_reaped(self):
        owner_pid, owner_start, group = self._dead_collector_with_group(ignore_sigterm=True)
        self._write_record(owner_pid, owner_start, group.pid, ac._proc_starttime(group.pid))
        self.assertTrue(ac._orca_reap_orphan_orca_groups())
        self.assertTrue(_wait_gone(group.pid), "a recorded group survived the reap")
        self.assertEqual(self._record_files(), [], "the reaped record was left behind")

    def test_a_live_collectors_group_is_never_reaped(self):
        """A record whose owner still runs belongs to an in-flight probe."""
        owner_pid, owner_start = self._dead_owner()
        live_owner = self._sleeping_group()
        owner_pid = live_owner.pid
        owner_start = ac._proc_starttime(owner_pid)
        group = self._sleeping_group(ignore_sigterm=True)
        self._write_record(owner_pid, owner_start, group.pid, ac._proc_starttime(group.pid))
        self.assertFalse(ac._orca_reap_orphan_orca_groups())
        time.sleep(0.2)
        self.assertFalse(_wait_gone(group.pid), "a live collector's group was reaped")
        live_owner.kill()
        live_owner.wait(timeout=5)
        self.assertTrue(ac._orca_reap_orphan_orca_groups())
        self.assertTrue(_wait_gone(group.pid), "the group survived once its owner was gone")

    def test_reap_treats_a_recycled_owner_pid_as_dead(self):
        """A live pid with a different start time is a different collector."""
        _old_pid, old_start = self._dead_owner()
        live_owner = self._sleeping_group()
        group = self._sleeping_group(ignore_sigterm=True)
        # The recorded owner start belongs to a collector that is gone; the
        # pid now belongs to an unrelated process and must not shield the group.
        self._write_record(live_owner.pid, old_start + 1, group.pid,
                           ac._proc_starttime(group.pid))
        self.assertTrue(ac._orca_reap_orphan_orca_groups())
        self.assertTrue(_wait_gone(group.pid), "a recycled owner pid blocked the reap")
        self.assertTrue(_alive(live_owner.pid), "the unrelated pid holder was killed")

    def test_record_is_cleared_when_the_group_is_already_gone(self):
        owner_pid, owner_start = self._dead_owner()
        group = self._sleeping_group()
        self._write_record(owner_pid, owner_start, group.pid, ac._proc_starttime(group.pid))
        group.kill()
        group.wait(timeout=5)
        self.assertFalse(ac._orca_reap_orphan_orca_groups())
        self.assertEqual(self._record_files(), [])

    def test_reap_ignores_a_record_whose_pid_was_reused(self):
        """The start time is the PID-reuse guard: a mismatch must kill nothing."""
        owner_pid, owner_start = self._dead_owner()
        group = self._sleeping_group()
        starttime = ac._proc_starttime(group.pid)
        self.assertIsNotNone(starttime)
        self._write_record(owner_pid, owner_start, group.pid, starttime + 1)
        self.assertFalse(ac._orca_reap_orphan_orca_groups())
        time.sleep(0.2)
        self.assertFalse(_wait_gone(group.pid), "a reused-pid record killed a live group")
        self.assertEqual(self._record_files(), [], "a stale record was left behind")

    def test_reap_ignores_a_record_from_another_boot(self):
        owner_pid, owner_start = self._dead_owner()
        group = self._sleeping_group()
        self._write_record(owner_pid, owner_start, group.pid, ac._proc_starttime(group.pid),
                           boot_id="00000000-0000-0000-0000-000000000000")
        self.assertFalse(ac._orca_reap_orphan_orca_groups())
        time.sleep(0.2)
        self.assertFalse(_wait_gone(group.pid), "a record from another boot killed a live group")
        self.assertEqual(self._record_files(), [])

    def test_reap_ignores_a_record_older_than_the_age_bound(self):
        owner_pid, owner_start = self._dead_owner()
        group = self._sleeping_group()
        old_ms = ac._uptime_ms() - int((ac._ORCA_JOURNAL_MAX_AGE + 60) * 1000)
        self._write_record(owner_pid, owner_start, group.pid, ac._proc_starttime(group.pid),
                           uptime_ms=old_ms)
        self.assertFalse(ac._orca_reap_orphan_orca_groups())
        time.sleep(0.2)
        self.assertFalse(_wait_gone(group.pid), "an expired record killed a live group")
        self.assertEqual(self._record_files(), [])

    def test_a_leader_recycled_during_the_reap_aborts_it(self):
        """The leader is re-read for every pass, not just once up front."""
        owner_pid, owner_start = self._dead_owner()
        group = self._sleeping_group(ignore_sigterm=True)
        starttime = ac._proc_starttime(group.pid)
        self._write_record(owner_pid, owner_start, group.pid, starttime)
        real = ac._proc_starttime

        def fake(pid):
            value = real(pid)
            if pid == group.pid and value is not None:
                return value + 1  # a recycled leader: different start time
            return value

        with mock.patch.object(ac, "_proc_starttime", side_effect=fake):
            self.assertFalse(ac._orca_reap_orphan_orca_groups())
        time.sleep(0.2)
        self.assertFalse(_wait_gone(group.pid), "a recycled leader did not abort the reap")

    def test_stale_temp_file_from_a_dead_collector_is_swept(self):
        owner_pid, _owner_start = self._dead_owner()
        stale = os.path.join(self.dir, ac._orca_journal_name(owner_pid) + ".tmp")
        with open(stale, "w") as f:
            f.write("half a record")
        self.assertTrue(os.path.exists(stale))
        ac._orca_reap_orphan_orca_groups()
        self.assertFalse(os.path.exists(stale), "a dead collector's temp file lingered")

    def test_stale_temp_of_a_recycled_owner_is_swept(self):
        live = self._sleeping_group()
        stale = os.path.join(
            self.dir,
            ac._orca_journal_name(live.pid, ac._proc_starttime(live.pid) + 1) + ".tmp")
        with open(stale, "w") as f:
            f.write("half a record")
        ac._orca_reap_orphan_orca_groups()
        self.assertFalse(os.path.exists(stale), "a recycled owner's temp file lingered")

    def test_clean_poll_clears_its_record(self):
        payload = '{"ok": true, "result": {"terminals": []}}'
        self._stub(f"sys.stdout.write({payload!r})\nsys.stdout.flush()\n")
        ac.query_orca_terminals(timeout=4.0)
        self.assertFalse(os.path.exists(self.journal),
                         "a clean poll left a record that would reap the runtime daemon")

    def test_parseable_non_success_is_a_clean_teardown(self):
        """A well-formed ok:false closed stdout: it is not a wedged helper."""
        good = [{"handle": "keep-me"}]
        stale = time.monotonic() - ac._ORCA_CACHE_TTL - 1.0
        ac._ORCA_CACHE.update({"ts": stale, "failed_ts": stale, "terminals": good})
        kidfile = os.path.join(self.dir, "kid.pid")
        self._stub(f"""
            kid = os.fork()
            if kid == 0:
                os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
                open({kidfile!r}, "w").write(str(os.getpid()))
                time.sleep(120)
                os._exit(0)
            while not os.path.exists({kidfile!r}):
                time.sleep(0.01)
            sys.stdout.write('{{"ok": false}}')
            sys.stdout.flush()
            sys.exit(0)
        """)
        self.assertEqual(ac.query_orca_terminals(timeout=4.0), good)
        with open(kidfile) as f:
            kid = int(f.read().strip())
        self.addCleanup(lambda: _alive(kid) and os.kill(kid, 9))
        self.assertEqual(ac._ORCA_CACHE["ts"], stale,
                         "a non-success reply poisoned the last-known-good roster")
        time.sleep(0.2)
        self.assertTrue(_alive(kid), "a parseable ok:false killed a runtime helper")
        self.assertFalse(os.path.exists(self.journal), "ok:false kept a stale record")

    def test_a_new_incarnation_does_not_clobber_an_unreaped_record(self):
        """A recycled pid keeps its old record instead of overwriting it."""
        owner = os.getpid()
        owner_start = ac._proc_starttime(owner)
        group = self._sleeping_group(ignore_sigterm=True)
        # The legacy name (no start-time segment) stands in for a record an
        # earlier collector with this pid left behind without being reaped.
        old_path = os.path.join(self.dir, ac._orca_journal_name(owner))
        with open(old_path, "w") as f:
            f.write(f"{ac._boot_id()} {ac._uptime_ms()} {group.pid} "
                    f"{ac._proc_starttime(group.pid)} {owner_start}\n")
        ac._orca_record_orphan(self._sleeping_group().pid)
        with open(old_path, encoding="ascii") as f:
            preserved = f.read().split()
        self.assertEqual(int(preserved[2]), group.pid,
                         "a recycled pid clobbered an unreaped orphan record")

    def test_a_live_sibling_is_spared_while_a_dead_collectors_group_is_reaped(self):
        """End-to-end: only dead owners' groups are reaped, live siblings survive."""
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
        collector = self._run_collector()
        dead_record = ac._orca_journal_path(collector.pid)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if os.path.exists(dead_record) and os.path.exists(kidfile):
                break
            time.sleep(0.02)
        else:
            self.fail("the collector never recorded its group")
        with open(kidfile) as f:
            dead_kid = int(f.read().strip())
        self.addCleanup(lambda: _alive(dead_kid) and os.kill(dead_kid, 9))

        sibling_owner = self._sleeping_group()
        sibling_group = self._sleeping_group(ignore_sigterm=True)
        sibling_path = self._write_record(
            sibling_owner.pid, ac._proc_starttime(sibling_owner.pid),
            sibling_group.pid, ac._proc_starttime(sibling_group.pid))
        payload = '{"ok": true, "result": {"terminals": []}}'
        self._stub(f"sys.stdout.write({payload!r})\nsys.stdout.flush()\n")

        # Both collectors are alive in this tick: neither group may be reaped.
        ac.query_orca_terminals(timeout=4.0)
        self.assertTrue(_alive(dead_kid), "a live collector's helper was reaped")
        self.assertTrue(_alive(sibling_group.pid), "a live sibling's group was reaped")

        collector.kill()
        collector.wait(timeout=10)
        ac._ORCA_CACHE["tick"] += 1
        ac.query_orca_terminals(timeout=4.0)
        self.assertTrue(_wait_gone(dead_kid), "the dead collector's helper survived")
        self.assertTrue(_alive(sibling_group.pid), "the live sibling's group was reaped")
        self.assertTrue(os.path.exists(sibling_path), "the live record was dropped")
        self.assertFalse(os.path.exists(dead_record), "the dead record was left behind")

    def test_zero_exit_with_invalid_output_kills_its_helper(self):
        """Garbage on a clean exit is NOT a clean teardown: the group still dies."""
        kidfile = os.path.join(self.dir, "kid.pid")
        self._stub(f"""
            kid = os.fork()
            if kid == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                os.dup2(os.open(os.devnull, os.O_WRONLY), 1)  # release the pipe
                open({kidfile!r}, "w").write(str(os.getpid()))
                time.sleep(120)
                os._exit(0)
            while not os.path.exists({kidfile!r}):
                time.sleep(0.01)
            sys.stdout.write("this is not json")
            sys.stdout.flush()
            sys.exit(0)          # clean exit, unusable payload
        """)
        self.assertEqual(ac.query_orca_terminals(timeout=4.0), [])
        with open(kidfile) as f:
            kid = int(f.read().strip())
        self.addCleanup(lambda: os.path.exists(f"/proc/{kid}") and os.kill(kid, 9))
        self.assertTrue(_wait_gone(kid), "invalid output was treated as a clean teardown")
        self.assertFalse(os.path.exists(self.journal), "the record outlived its cleanup")

    # --- journal directory validation -------------------------------------

    def test_journaling_is_disabled_for_a_group_readable_directory(self):
        # 0755 has no group/other WRITE bit, so the ancestor rule allows it and
        # only the held directory's owner-only rule can reject it.
        os.chmod(self.dir, 0o755)
        self.addCleanup(os.chmod, self.dir, 0o700)
        self.assertIsNone(ac._orca_journal_hold_dir(),
                          "a group-readable runtime dir was trusted")
        self._stub("time.sleep(120)\n")
        ac.query_orca_terminals(timeout=0.4)  # still works, just unjournaled
        self.assertEqual(self._spawn_count(), 1)
        self.assertEqual(self._record_files(), [])

    def test_journal_dir_that_is_a_symlink_is_refused(self):
        real = os.path.join(self.dir, "real")
        os.mkdir(real)
        link = os.path.join(self.dir, "link")
        os.symlink(real, link)
        ac._ORCA_JOURNAL_DIR = link
        self.assertIsNone(ac._orca_journal_hold_dir(), "a symlinked runtime dir was trusted")

    def test_journal_dir_under_a_writable_ancestor_is_refused(self):
        outer = os.path.join(self.dir, "outer")
        inner = os.path.join(outer, "inner")
        os.makedirs(inner, mode=0o700)
        os.chmod(outer, 0o777)
        ac._ORCA_JOURNAL_DIR = inner
        self.assertIsNone(ac._orca_journal_path(), "a writable ancestor was trusted")
        self.assertIsNone(ac._orca_journal_hold_dir(), "a writable ancestor was trusted")

    def test_journal_dir_under_a_symlinked_ancestor_is_refused(self):
        real = os.path.join(self.dir, "real")
        sub = os.path.join(real, "sub")
        os.makedirs(sub, mode=0o700)
        link = os.path.join(self.dir, "link")
        os.symlink(real, link)
        ac._ORCA_JOURNAL_DIR = os.path.join(link, "sub")
        self.assertIsNone(ac._orca_journal_hold_dir(),
                          "a symlinked ancestor was trusted")

    def test_journal_dir_owned_by_another_uid_is_refused(self):
        with mock.patch.object(ac.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertIsNone(ac._orca_journal_path(),
                              "a runtime dir owned by someone else was trusted")
            self.assertIsNone(ac._orca_journal_hold_dir(),
                              "a runtime dir owned by someone else was trusted")

    def test_journal_dir_that_is_relative_is_refused(self):
        os.mkdir(os.path.join(self.dir, "runtime"), 0o700)
        previous = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, previous)
        ac._ORCA_JOURNAL_DIR = "runtime"
        self.assertIsNone(ac._orca_journal_path(), "a relative runtime dir was trusted")
        self.assertIsNone(ac._orca_journal_hold_dir(), "a relative runtime dir was trusted")

    def test_journal_dir_that_is_a_regular_file_is_refused(self):
        not_a_dir = os.path.join(self.dir, "not-a-dir")
        with open(not_a_dir, "w") as f:
            f.write("")
        os.chmod(not_a_dir, 0o600)
        ac._ORCA_JOURNAL_DIR = not_a_dir
        self.assertIsNone(ac._orca_journal_hold_dir(), "a non-directory runtime path was trusted")

    # --- record content guards --------------------------------------------

    def test_journal_record_that_is_not_a_regular_file_is_ignored(self):
        os.mkdir(self.journal)
        self.assertIsNone(ac._orca_read_orphan())
        self._stub("time.sleep(120)\n")
        ac.query_orca_terminals(timeout=0.4)  # must not raise, just unjournaled

    def test_oversized_journal_record_is_ignored(self):
        with open(self.journal, "w") as f:
            f.write("x" * (ac._ORCA_JOURNAL_MAX_BYTES + 40))
        self.assertIsNone(ac._orca_read_orphan())

    def test_symlinked_record_is_never_followed(self):
        owner_pid, owner_start = self._dead_owner()
        group = self._sleeping_group(ignore_sigterm=True)
        path = self._write_record(owner_pid, owner_start, group.pid,
                                  ac._proc_starttime(group.pid))
        real = os.path.join(self.dir, "real-record")
        os.replace(path, real)          # move the genuine record aside...
        os.symlink(real, path)          # ...and point the record path at it
        self.assertFalse(ac._orca_reap_orphan_orca_groups())
        time.sleep(0.2)
        self.assertFalse(_wait_gone(group.pid), "a symlinked record was followed")

    # --- end-to-end: the collector dies, the next probe reaps --------------

    def _run_collector(self, timeout=30):
        log = os.path.join(self.dir, "collector.log")
        collector = textwrap.dedent(f"""
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "ac", {os.path.join(REPO_ROOT, 'agent_ctl.py')!r})
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            m.ORCA_CLI = {os.path.join(self.dir, 'stub.py')!r}
            m._ORCA_JOURNAL_DIR = {self.dir!r}
            open({log!r}, "w").write("ready")
            m.query_orca_terminals(timeout={timeout})
        """)
        proc = subprocess.Popen([sys.executable, "-c", collector])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def test_helper_left_by_a_killed_collector_is_reaped_by_the_next_probe(self):
        """The block's exact end-to-end case, with the kill it needs to survive."""
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
        collector_proc = self._run_collector()
        record = ac._orca_journal_path(collector_proc.pid)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if os.path.exists(kidfile) and os.path.exists(record):
                break
            time.sleep(0.02)
        else:
            self.fail("the collector never spawned the stub and recorded its group")
        with open(kidfile) as f:
            kid = int(f.read().strip())
        self.addCleanup(lambda: os.path.exists(f"/proc/{kid}") and os.kill(kid, 9))
        leader = self._stub_pid()
        collector_proc.kill()
        collector_proc.wait(timeout=10)
        self.assertTrue(_wait_gone(leader), "PDEATHSIG missed the direct CLI")
        self.assertTrue(_alive(kid), "the forked helper was expected to survive a SIGKILL")

        payload = '{"ok": true, "result": {"terminals": []}}'
        self._stub(f"sys.stdout.write({payload!r})\nsys.stdout.flush()\n")
        ac.query_orca_terminals(timeout=4.0)
        self.assertTrue(_wait_gone(kid), "a helper orphaned by a killed collector was never reaped")
        self.assertFalse(os.path.exists(record), "the reaped record was left behind")

    def test_record_survives_until_the_call_is_verified_clean(self):
        """EOF alone must not clear the record: the group can still be alive.

        The CLI closes stdout, forks a SIGTERM-ignoring helper and keeps
        running. The collector sees EOF and blocks in wait(); killing it there
        used to leave the helper with no record at all.
        """
        kidfile = os.path.join(self.dir, "kid.pid")
        eof_marker = os.path.join(self.dir, "eof.marker")
        self._stub(f"""
            kid = os.fork()
            if kid == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                open({kidfile!r}, "w").write(str(os.getpid()))
                time.sleep(120)
                os._exit(0)
            while not os.path.exists({kidfile!r}):
                time.sleep(0.01)
            os.close(1)                     # EOF, with no payload written
            open({eof_marker!r}, "w").close()
            time.sleep(120)                 # leader stays alive after EOF
        """)
        collector_proc = self._run_collector()
        record = ac._orca_journal_path(collector_proc.pid)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if (os.path.exists(eof_marker) and os.path.exists(record)
                    and os.path.exists(kidfile) and os.path.getsize(kidfile) > 0):
                break
            time.sleep(0.02)
        else:
            self.fail("the collector never reached EOF with a recorded group")
        with open(kidfile) as f:
            kid = int(f.read().strip())
        self.addCleanup(lambda: os.path.exists(f"/proc/{kid}") and os.kill(kid, 9))
        time.sleep(0.1)  # land the kill inside wait(), after EOF
        collector_proc.kill()
        collector_proc.wait(timeout=10)
        self.assertTrue(os.path.exists(record),
                        "EOF cleared the only record of a still-live group")
        self.assertTrue(_alive(kid), "the helper was expected to survive a SIGKILL")
        payload = '{"ok": true, "result": {"terminals": []}}'
        self._stub(f"sys.stdout.write({payload!r})\nsys.stdout.flush()\n")
        ac.query_orca_terminals(timeout=4.0)
        self.assertTrue(_wait_gone(kid), "the post-EOF helper was never reaped")
        self.assertFalse(os.path.exists(record))

    # --- signalling -------------------------------------------------------

    def test_ambiguous_death_reaps_every_member_it_recorded(self):
        """Pinned trade-off: a record left by a kill mid-call is reaped whole."""
        kidfile = os.path.join(self.dir, "kid.pid")
        script = textwrap.dedent(f"""
            import os, signal, time
            kid = os.fork()
            if kid == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                time.sleep(120)
                os._exit(0)
            open({kidfile!r}, 'w').write(str(kid))
            time.sleep(120)
        """)
        proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if os.path.exists(kidfile) and os.path.getsize(kidfile) > 0:
                break
            time.sleep(0.01)
        with open(kidfile) as f:
            kid = int(f.read().strip())
        self.addCleanup(lambda: os.path.exists(f"/proc/{kid}") and os.kill(kid, 9))
        self.assertEqual(ac._proc_pgrp(kid), proc.pid, "the helper is not a group member")
        owner_pid, owner_start = self._dead_owner()
        self._write_record(owner_pid, owner_start, proc.pid, ac._proc_starttime(proc.pid))
        self.assertTrue(ac._orca_reap_orphan_orca_groups())
        self.assertTrue(_wait_gone(proc.pid), "the recorded leader survived the reap")
        self.assertTrue(_wait_gone(kid), "a member of the recorded group survived the reap")
        self.assertEqual(self._record_files(), [])

    def test_member_that_left_the_group_is_not_signalled(self):
        """Per-member identity: a pid in another group is never signalled."""
        proc = self._sleeping_group()
        self.assertFalse(
            ac._signal_orca_group_member(proc.pid, proc.pid + 1, signal.SIGKILL),
            "a pid outside the recorded group was signalled",
        )
        time.sleep(0.2)
        self.assertFalse(_wait_gone(proc.pid), "a non-member was killed")

    def test_pidfd_open_unavailable_never_signals(self):
        """No pidfd, no signal: the numeric kill fallback is not allowed."""
        proc = self._sleeping_group()
        with mock.patch.object(ac, "_PIDFD_OPEN", None):
            self.assertFalse(ac._signal_orca_group_member(proc.pid, proc.pid, signal.SIGKILL))
        time.sleep(0.2)
        self.assertFalse(_wait_gone(proc.pid), "signalled without a pidfd to pin the pid")

    def test_pidfd_send_unavailable_never_signals(self):
        proc = self._sleeping_group()
        with mock.patch.object(ac, "_PIDFD_SEND", None):
            self.assertFalse(ac._signal_orca_group_member(proc.pid, proc.pid, signal.SIGKILL))
        time.sleep(0.2)
        self.assertFalse(_wait_gone(proc.pid), "signalled without pidfd_send")


if __name__ == "__main__":
    unittest.main(verbosity=2)
