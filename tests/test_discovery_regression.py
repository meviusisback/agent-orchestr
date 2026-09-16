#!/usr/bin/env python3
"""Focused discovery regressions: local Hermes multiplicity and remote Dev Claude."""

import importlib.util
import json
import os
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("agent_ctl_under_test", os.path.join(ROOT, "agent_ctl.py"))
assert spec and spec.loader
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)


class TestDiscoveryRegression(unittest.TestCase):
    def test_keeps_multiple_local_hermes_desktop_instances(self):
        processes = {
            101: {"pid": 101, "ppid": 1, "cmd": "/opt/Hermes", "argv": ["/opt/Hermes"], "cwd": "/tmp", "state": "S"},
            102: {"pid": 102, "ppid": 1, "cmd": "/opt/Hermes", "argv": ["/opt/Hermes"], "cwd": "/tmp", "state": "S"},
        }
        def proc_glob(pattern):
            return ["/proc/101", "/proc/102"] if pattern == "/proc/[0-9]*" else []
        def session_info(**kwargs):
            return (f"task-{kwargs.get('hermes_home')}", "model", "provider", "profile", "detail", "working", False)
        with mock.patch.object(ac.glob, "glob", side_effect=proc_glob), \
             mock.patch.object(ac, "get_process_info", side_effect=lambda pid: processes[int(pid)]), \
             mock.patch.object(ac, "get_process_ancestors", return_value=[]), \
             mock.patch.object(ac, "get_process_hermes_home", side_effect=lambda pid: f"/tmp/hermes-{pid}"), \
             mock.patch.object(ac, "extract_hermes_session_info", side_effect=session_info), \
             mock.patch.object(ac, "get_hypr_clients", return_value=[]):
            rows = ac.scan_standalone_agents([], set(), set())
        self.assertEqual([row["pane_id"] for row in rows], ["desktop:hermes:101", "desktop:hermes:102"])
        self.assertEqual([row["title"] for row in rows], ["task-/tmp/hermes-101", "task-/tmp/hermes-102"])

    def test_collects_claude_from_remote_dev_snapshot(self):
        snapshot = {
            "workspaces": [{"workspace_id": "w1", "label": "AnyListPlus"}],
            "tabs": [{"tab_id": "w1:t1", "label": "Claude"}],
            "panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "cwd": "/home/izckk/Develop/Projects/AnyListPlus"}],
            "agents": [{"pane_id": "w1:p1", "agent": "claude", "agent_status": "working"}],
        }
        response = {"result": {"snapshot": snapshot}}
        machine = {"id": "dev-id", "label": "Dev", "target": "dev", "session": "default"}
        with mock.patch.object(ac, "get_herdr_server_pids", return_value=[]), \
             mock.patch.object(ac, "herdr_session_sockets", return_value=[]), \
             mock.patch.object(ac, "herdr_machine_list", return_value=[machine]), \
             mock.patch.object(ac, "query_remote_herdr_snapshot", return_value=response), \
             mock.patch.object(ac, "scan_orca_agents", return_value=[]), \
             mock.patch.object(ac, "scan_standalone_agents", return_value=[]), \
             mock.patch.object(ac, "query_orca_terminals", return_value=[]):
            data = ac.fetch_all_agents()
        self.assertEqual(len(data["agents"]), 1)
        self.assertEqual(data["agents"][0]["agent"], "claude")
        self.assertEqual(data["agents"][0]["origin_label"], "Herdr · Dev")
        self.assertEqual(data["agents"][0]["status"], "working")


if __name__ == "__main__":
    unittest.main()
