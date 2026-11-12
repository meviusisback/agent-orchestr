#!/usr/bin/env python3
"""Fixture tests for kill_target() dispatch in agent_ctl.py.

Hermetic by construction: Hyprland queries, the Herdr socket, window closing
and os.kill are all patched, so the suite signals nothing, spawns no hyprctl
and talks to no socket. Safe to execute on a live desktop with real agents
running.

Run:  python3 tests/test_kill_target.py
"""

import importlib.util
import os
import signal
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_agent_ctl():
    """Import agent_ctl.py from the repo without installing it as a package."""
    spec = importlib.util.spec_from_file_location("agent_ctl_under_test", os.path.join(REPO_ROOT, "agent_ctl.py"))
    assert spec is not None and spec.loader is not None, "cannot load agent_ctl.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ac = _load_agent_ctl()

WINDOW_ADDR = "0x55f0aa11"
TERM_PID = 4242
AGENT_PID = 4243


class KillTargetCase(unittest.TestCase):
    """Patch out every side effect kill_target() can reach."""

    def setUp(self):
        self.signalled = []
        self.closed = []
        self.socket_calls = []

        self.clients = [{"address": WINDOW_ADDR, "pid": TERM_PID, "class": "com.mitchellh.ghostty"}]
        self.window_agents = {TERM_PID: [AGENT_PID]}
        self.socket_reply = {"id": "test", "result": {"closed": True}}

        def fake_kill(pid, sig):
            self.assertEqual(sig, signal.SIGTERM)
            self.signalled.append(pid)

        def fake_close(addr, env=None):
            self.closed.append(addr)
            return True

        def fake_socket(method, params=None, timeout=1.0, sock_path=None):
            self.socket_calls.append((method, params))
            return self.socket_reply

        for target, replacement in (
            ("get_hypr_env", lambda: {}),
            ("get_hypr_clients", lambda: self.clients),
            ("agent_pids_in_window", lambda pid: self.window_agents.get(pid, [])),
            ("close_hypr_window", fake_close),
            ("query_herdr_socket", fake_socket),
        ):
            patcher = mock.patch.object(ac, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        patcher = mock.patch.object(ac.os, "kill", fake_kill)
        patcher.start()
        self.addCleanup(patcher.stop)


class TerminalAddressTargets(KillTargetCase):
    """Standalone cards are keyed by window address, so that form must work."""

    def test_signals_agent_in_window_and_closes_it(self):
        res = ac.kill_target(f"terminal:addr:{WINDOW_ADDR}")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["killed_pids"], [AGENT_PID])
        self.assertEqual(self.signalled, [AGENT_PID])
        self.assertEqual(self.closed, [WINDOW_ADDR])

    def test_never_forwards_a_window_address_to_herdr(self):
        ac.kill_target(f"terminal:addr:{WINDOW_ADDR}")
        self.assertEqual(self.socket_calls, [])

    def test_unknown_window_is_a_failure(self):
        self.clients = []
        res = ac.kill_target(f"terminal:addr:{WINDOW_ADDR}")
        self.assertFalse(res["ok"])
        self.assertEqual(self.signalled, [])

    def test_window_without_recognized_agent_is_a_failure(self):
        self.window_agents = {}
        res = ac.kill_target(f"terminal:addr:{WINDOW_ADDR}")
        self.assertFalse(res["ok"])
        self.assertEqual(self.signalled, [])
        self.assertEqual(self.closed, [])

    def test_malformed_address_is_rejected(self):
        res = ac.kill_target("terminal:addr:not-an-address")
        self.assertFalse(res["ok"])
        self.assertEqual(self.signalled, [])


class HerdrTargets(KillTargetCase):
    """A pane that Herdr refused to close must not report success."""

    def test_successful_pane_close(self):
        res = ac.kill_target("herdr:default|w1:p5")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.socket_calls, [("pane.close", {"pane_id": "w1:p5"})])

    def test_rpc_error_is_reported(self):
        self.socket_reply = {"id": "test", "error": {"code": "pane_not_found", "message": "pane w1:p5 not found"}}
        res = ac.kill_target("herdr:default|w1:p5")
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])

    def test_silent_socket_is_reported(self):
        self.socket_reply = None
        res = ac.kill_target("herdr:default|w1:p5")
        self.assertFalse(res["ok"])

    def test_unmatched_prefix_error_is_reported(self):
        self.socket_reply = {"id": "test", "error": {"code": "pane_not_found", "message": "pane orca:term:1 not found"}}
        res = ac.kill_target("orca:term:1")
        self.assertFalse(res["ok"])


class HerdrCoreTargets(KillTargetCase):
    """0.1 generation: `terminal.kill` over the core socket, not pane.close."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(ac, "herdr_core_sockets", return_value=["/run/user/1000/herdr-core/default.sock"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_successful_terminal_kill(self):
        res = ac.kill_target("herdr-core:default:t7")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.socket_calls, [("terminal.kill", {"terminal": "t7"})])

    def test_rpc_error_is_reported(self):
        self.socket_reply = {"id": "test", "error": {"code": -32000, "message": "no terminal \"t7\""}}
        res = ac.kill_target("herdr-core:default:t7")
        self.assertFalse(res["ok"])
        self.assertIn("no terminal", res["error"])

    def test_malformed_address_is_rejected(self):
        res = ac.kill_target("herdr-core:default:t7; rm -rf /")
        self.assertFalse(res["ok"])
        self.assertEqual(self.socket_calls, [])

    def test_missing_core_socket_is_reported(self):
        with mock.patch.object(ac, "herdr_core_sockets", return_value=[]):
            res = ac.kill_target("herdr-core:default:t7")
        self.assertFalse(res["ok"])
        self.assertEqual(self.socket_calls, [])

    def test_bare_address_is_refused_while_several_cores_are_live(self):
        # Two cores both own a 't7': without a core in the target there is no
        # safe choice, so the kill is refused instead of guessing.
        calls = []
        self.socket_send = lambda method, params: calls.append((method, params))
        with mock.patch.object(ac, "herdr_core_sockets",
                               return_value=["/run/user/1000/herdr-core/aaa.sock",
                                             "/run/user/1000/herdr-core/zzz.sock"]):
            res = ac.kill_target("herdr-core:t7")
        self.assertFalse(res["ok"])
        self.assertIn("must name its core", res["error"])
        self.assertEqual(calls, [])

    def test_kill_goes_only_to_the_card_core(self):
        # The card came from zzz; aaa refuses the same address. The kill must
        # never fall through to aaa (that terminated an unrelated agent and
        # reported success for the card the user clicked).
        seen = []

        def fake_query(method, params=None, timeout=1.0, sock_path=None):
            seen.append((sock_path, method, params))
            if sock_path.endswith("aaa.sock"):
                return {"id": "x", "error": {"code": -32000, "message": 'no terminal "t7"'}}
            return {"id": "x", "result": {}}

        with mock.patch.object(ac, "herdr_core_sockets",
                               return_value=["/run/user/1000/herdr-core/aaa.sock",
                                             "/run/user/1000/herdr-core/zzz.sock"]), \
             mock.patch.object(ac, "query_herdr_socket", side_effect=fake_query):
            res = ac.kill_target("herdr-core:zzz:t7")
        self.assertTrue(res["ok"], res)
        self.assertEqual([s for s, _m, _p in seen], ["/run/user/1000/herdr-core/zzz.sock"])

    def test_refusal_on_the_card_core_is_not_retried_elsewhere(self):
        def fake_query(method, params=None, timeout=1.0, sock_path=None):
            return {"id": "x", "error": {"code": -32000, "message": 'no terminal "t7"'}}

        with mock.patch.object(ac, "herdr_core_sockets",
                               return_value=["/run/user/1000/herdr-core/aaa.sock",
                                             "/run/user/1000/herdr-core/zzz.sock"]), \
             mock.patch.object(ac, "query_herdr_socket", side_effect=fake_query):
            res = ac.kill_target("herdr-core:zzz:t7")
        self.assertFalse(res["ok"])
        self.assertIn("no terminal", res["error"])


class ProcessTargets(KillTargetCase):
    """The PID-addressed paths keep their agent validation gate."""

    def test_foreign_pid_is_refused(self):
        with mock.patch.object(ac, "is_valid_agent_process", lambda pid: False):
            res = ac.kill_target(f"terminal:pid:{AGENT_PID}")
        self.assertFalse(res["ok"])
        self.assertEqual(self.signalled, [])

    def test_validated_pid_is_signalled(self):
        with mock.patch.object(ac, "is_valid_agent_process", lambda pid: True), \
             mock.patch.object(ac, "get_process_ancestors", lambda pid, max_depth=20: []), \
             mock.patch.object(ac, "match_hypr_client_for_terminal", lambda pids, cwd, agent: self.clients[0]):
            res = ac.kill_target(f"terminal:pid:{AGENT_PID}")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.signalled, [AGENT_PID])
        self.assertEqual(self.closed, [WINDOW_ADDR])

    def test_missing_target_is_refused(self):
        res = ac.kill_target("")
        self.assertFalse(res["ok"])


class AgentPidResolution(unittest.TestCase):
    """agent_pids_in_window() must only ever return validated descendants."""

    def test_collects_validated_descendants_only(self):
        tree = {AGENT_PID: [{"pid": TERM_PID}], 9001: [{"pid": 1}]}
        with mock.patch.object(ac.glob, "glob", lambda pattern: [f"/proc/{AGENT_PID}", "/proc/9001", "/proc/self"]), \
             mock.patch.object(ac, "is_valid_agent_process", lambda pid: pid in (AGENT_PID, 9001)), \
             mock.patch.object(ac, "get_process_ancestors", lambda pid, max_depth=20: tree.get(pid, [])):
            self.assertEqual(ac.agent_pids_in_window(TERM_PID), [AGENT_PID])

    def test_rejects_unusable_window_pid(self):
        self.assertEqual(ac.agent_pids_in_window(1), [])
        self.assertEqual(ac.agent_pids_in_window(None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
