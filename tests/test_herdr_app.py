#!/usr/bin/env python3
"""Hermetic tests for Herdr app (herdr-core / hc) agent discovery, focus and kill."""

import importlib.util
import os
import subprocess
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("agent_ctl_under_test", os.path.join(ROOT, "agent_ctl.py"))
assert spec and spec.loader
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)

LS = {"terminals": [
    {"address": "t3", "agent": "bash", "cwd": "/home/u", "state": "live", "where": "ws / 1"},
    {"address": "t5", "agent": "claude-code", "cwd": "/home/u", "state": "live", "status": "blocked", "where": "ws / Claude"},
    {"address": "box/t1", "agent": "codex", "cwd": "/root", "state": "live", "status": "working", "where": "srv / 1"},
    {"address": "t9", "agent": "claude-code", "cwd": "/x", "state": "exited", "status": "idle"},
    {"address": "t1; rm -rf /", "agent": "claude-code", "cwd": "/x", "state": "live", "status": "idle"},
]}
GET = {
    "t3": {"address": "t3", "state": "live", "title": "x - grok", "cwd": "/home/u", "foreground": {"name": "grok", "cwd": "/home/u"}, "status": None},
    "t5": {"address": "t5", "state": "live", "title": "Fix login", "cwd": "/home/u", "foreground": {"cwd": "/home/u/app"},
           "status": {"app": "claude-code", "state": "blocked", "msg": "needs approval sk-ant-api03-" + "a" * 40},
           "labels": {"where": "ws / Claude"}},
}


def fake_hc(args, timeout=0):
    if args[0] == "ls":
        return LS
    return GET.get(args[1])


class TestHerdrAppCards(unittest.TestCase):
    def cards(self):
        with mock.patch.object(ac, "hc_run_json", side_effect=fake_hc):
            return {c["raw_pane_id"]: c for c in ac.collect_herdr_app_cards()}

    def test_only_live_agents_with_valid_addresses(self):
        self.assertEqual(set(self.cards()), {"t3", "t5", "box/t1"})

    def test_grok_found_by_foreground_program(self):
        with mock.patch.object(ac, "find_latest_session_for_cwd", return_value=None):
            c = self.cards()["t3"]
        self.assertEqual((c["agent"], c["status"]), ("grok", "idle"))

    def test_status_and_fields(self):
        c = self.cards()["t5"]
        self.assertEqual((c["agent"], c["status"], c["title"]), ("claude", "waiting", "Fix login"))
        self.assertEqual((c["workspace"], c["tab"], c["pane_id"]), ("ws", "Claude", "herdr-app:t5"))
        self.assertTrue(c["can_control"])
        self.assertNotIn("sk-ant-api03", c["detail"])

    def test_remote_is_read_only_for_kill(self):
        c = self.cards()["box/t1"]
        self.assertEqual(c["origin"], "herdr_app_remote")
        self.assertFalse(c["can_control"])
        self.assertFalse(ac.kill_target("herdr-app:box/t1")["ok"])

    def test_hc_missing_yields_no_cards(self):
        with mock.patch.object(ac.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(ac.collect_herdr_app_cards(), [])


class TestHerdrAppControl(unittest.TestCase):
    def test_kill_rejects_bad_address(self):
        with mock.patch.object(ac.subprocess, "run") as run:
            self.assertFalse(ac.kill_target("herdr-app:t1; reboot")["ok"])
            run.assert_not_called()

    def test_kill_runs_hc_kill(self):
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(ac.subprocess, "run", return_value=done) as run:
            self.assertTrue(ac.kill_target("herdr-app:t5")["ok"])
            self.assertEqual(run.call_args[0][0], ["hc", "kill", "t5"])

    def test_focus_calls_agent_focus_then_window(self):
        done = subprocess.CompletedProcess([], 0, "", "")
        win = {"class": "herdr-app", "title": "Herdr", "workspace": {"id": 2}}
        with mock.patch.object(ac.subprocess, "run", return_value=done) as run, \
             mock.patch.object(ac, "get_hypr_clients", return_value=[win]), \
             mock.patch.object(ac, "focus_hypr_window") as fw:
            res = ac.focus_pane("herdr-app:t5")
        self.assertTrue(res["ok"])
        self.assertEqual(run.call_args[0][0][:3], ["herdr-app", "do", "agent.focus"])
        fw.assert_called_once_with(win)

    def test_focus_rejects_bad_address(self):
        self.assertFalse(ac.focus_pane("herdr-app:../etc")["ok"])


if __name__ == "__main__":
    unittest.main()
