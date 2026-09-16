#!/usr/bin/env python3
"""Regression checks for Hermes profile scoping and Herdr activity status."""

import importlib.util
import os
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("agent_ctl_under_test", os.path.join(ROOT, "agent_ctl.py"))
assert spec and spec.loader
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)


class TestHermesActivity(unittest.TestCase):
    def test_process_home_reads_real_nul_delimiters(self):
        handle = mock.mock_open(read_data=b"HOME=/tmp" + b"\0" + b"HERMES_HOME=/tmp/hermes" + b"\0")
        with mock.patch("builtins.open", handle):
            self.assertEqual(ac.get_process_hermes_home(123), "/tmp/hermes")

    def test_profile_db_is_selected_for_concurrent_process(self):
        with mock.patch.object(ac.os.path, "exists", return_value=True):
            rows = ac.get_all_hermes_dbs("/tmp/hermes", "fable")
        self.assertEqual(rows, [("/tmp/hermes/profiles/fable/state.db", "fable")])

    def test_missing_requested_profile_does_not_fallback_to_other_databases(self):
        with mock.patch.object(ac.os.path, "exists", side_effect=lambda path: path.endswith("/state.db") and "missing" not in path):
            self.assertEqual(ac.get_all_hermes_dbs("/tmp/hermes", "missing"), [])

    def test_desktop_child_server_is_labeled_desktop_not_herdr(self):
        processes = {
            200: {"pid": 200, "ppid": 1, "cmd": "/opt/Hermes --disable-setuid-sandbox", "argv": ["/opt/Hermes"], "cwd": "/tmp", "state": "S"},
            201: {"pid": 201, "ppid": 200, "cmd": "python -m hermes_cli.main --profile fable serve --host 127.0.0.1", "argv": ["python", "-m", "hermes_cli.main", "--profile", "fable", "serve", "--host", "127.0.0.1"], "cwd": "/tmp", "state": "S"},
        }
        with mock.patch.object(ac.glob, "glob", return_value=["/proc/201"]), \
             mock.patch.object(ac, "get_process_info", return_value=processes[201]), \
             mock.patch.object(ac, "get_process_ancestors", return_value=[processes[200]]), \
             mock.patch.object(ac, "get_process_hermes_home", return_value="/tmp/hermes"), \
             mock.patch.object(ac, "get_process_start_time", return_value=0), \
             mock.patch.object(ac, "extract_hermes_session_info", return_value=(None, None, None, None, "Ready", "idle", False)):
            rows = ac.scan_standalone_agents([], set(), set())
        self.assertEqual(rows[0]["origin"], "desktop")
        self.assertEqual(rows[0]["origin_label"], "Hermes Desktop")
        self.assertEqual(rows[0]["agent_display"], "Hermes Desktop")
        self.assertEqual(rows[0]["tab"], "Hermes Desktop (PID 201)")

    def test_unknown_origin_is_not_herdr_summary_category(self):
        model = os.path.join(ROOT, "Model.js")
        with open(model, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('var unknownCount = 0', text)
        self.assertIn('unknownCount++', text)
        self.assertIn('unknown agents', text)

    def test_profile_flag_forms_are_parsed(self):
        for argv in (["hermes", "--profile", "fable"], ["hermes", "--profile=fable"], ["hermes", "-p", "fable"], ["hermes", "-pfable"]):
            self.assertEqual(ac.hermes_profile_from_argv(argv), "fable")

    def test_herdr_status_mapping_preserves_unknown_and_blocked_attention(self):
        self.assertEqual(ac.normalize_herdr_status("blocked"), "waiting")
        self.assertEqual(ac.normalize_herdr_status("unknown"), "unknown")
        self.assertEqual(ac.normalize_herdr_status("new_status"), "unknown")

    def test_hermes_tui_label_and_screen_status_are_normalized(self):
        self.assertEqual(ac.normalize_hermes_agent("hermes tui"), "hermes")
        self.assertEqual(ac.hermes_screen_status("\u23f3 Running tool: terminal"), "working")
        self.assertEqual(ac.hermes_screen_status("Ready for prompt"), "idle")
        self.assertEqual(ac.hermes_screen_status("Enter to confirm"), "waiting")

    def test_remote_hermes_screen_overrides_stale_idle_detector(self):
        snapshot = {
            "workspaces": [{"workspace_id": "w1", "label": "Remote"}],
            "tabs": [{"tab_id": "w1:t1", "label": "1"}],
            "panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "cwd": "/tmp"}],
            "agents": [{"pane_id": "w1:p1", "agent": "hermes tui", "agent_status": "idle"}],
        }
        machine = {"id": "m1", "label": "Dev", "target": "dev", "session": "default"}
        response = {"result": {"snapshot": snapshot}}
        with mock.patch.object(ac, "get_herdr_server_pids", return_value=[]), \
             mock.patch.object(ac, "herdr_session_sockets", return_value=[]), \
             mock.patch.object(ac, "herdr_machine_list", return_value=[machine]), \
             mock.patch.object(ac, "query_remote_herdr_snapshot", return_value=response), \
             mock.patch.object(ac, "query_remote_herdr_agent_read", return_value="⏳ Running tool"), \
             mock.patch.object(ac, "scan_orca_agents", return_value=[]), \
             mock.patch.object(ac, "scan_standalone_agents", return_value=[]), \
             mock.patch.object(ac, "query_orca_terminals", return_value=[]):
            data = ac.fetch_all_agents()
        self.assertEqual(data["agents"][0]["agent"], "hermes")
        self.assertEqual(data["agents"][0]["status"], "working")


if __name__ == "__main__":
    unittest.main()
