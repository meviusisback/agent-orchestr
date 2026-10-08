#!/usr/bin/env python3
"""Focused discovery regressions: local Hermes multiplicity and remote Dev Claude."""

import importlib.util
import io
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


class TestHermesDesktopDedup(unittest.TestCase):
    """Issue #24: launcher parent, Wayland app child and crashpad must not
    each become a desktop:hermes card. Real shapes in fixtures/issue24-host-capture.json."""

    GU = "/home/alberto/.hermes/hermes-agent/apps/desktop/release/linux-unpacked"

    def _gui(self, pid, ppid, *args):
        argv = [f"{self.GU}/Hermes", *args]
        return {"pid": pid, "ppid": ppid, "cmd": " ".join(argv), "argv": argv,
                "cwd": "/tmp", "state": "S"}

    def _scan(self, processes, kids):
        def ancestors(pid):
            out = []
            seen = set()
            curr = processes[int(pid)]["ppid"]
            while curr in processes and curr not in seen:
                seen.add(curr)
                out.append(processes[curr])
                curr = processes[curr]["ppid"]
            return out

        def session_info(**kwargs):
            return ("task", "model", "provider", "profile", "detail", "working", False)

        with mock.patch.object(ac.glob, "glob",
                               side_effect=lambda pat: [f"/proc/{p}" for p in processes]
                               if pat == "/proc/[0-9]*" else []), \
             mock.patch.object(ac, "get_process_info", side_effect=lambda pid: processes[int(pid)]), \
             mock.patch.object(ac, "get_process_ancestors", side_effect=ancestors), \
             mock.patch.object(ac, "hermes_desktop_gui_child_pids",
                               side_effect=lambda pid: kids.get(int(pid), [])), \
             mock.patch.object(ac, "get_process_hermes_home", return_value="/tmp/hermes"), \
             mock.patch.object(ac, "extract_hermes_session_info", side_effect=session_info), \
             mock.patch.object(ac, "get_hypr_clients", return_value=[]):
            return ac.scan_standalone_agents([], set(), set())

    def test_launcher_wayland_child_and_crashpad_yield_single_card(self):
        launcher = self._gui(11, 1, "--disable-setuid-sandbox")
        app = self._gui(12, 11, "--disable-setuid-sandbox", "--ozone-platform=wayland")
        crashpad = {"pid": 13, "ppid": 1,
                    "cmd": (f"{self.GU}/chrome_crashpad_handler --monitor-self "
                            "--database=/home/alberto/.config/Hermes/Crashpad"),
                    "argv": [f"{self.GU}/chrome_crashpad_handler", "--monitor-self"],
                    "cwd": "/tmp", "state": "S"}
        rows = self._scan({11: launcher, 12: app, 13: crashpad},
                          {11: [12], 12: [], 13: []})
        self.assertEqual([r["pane_id"] for r in rows], ["desktop:hermes:12"])

    def test_crashpad_alone_yields_no_desktop_card(self):
        crashpad = {"pid": 13, "ppid": 1,
                    "cmd": (f"{self.GU}/chrome_crashpad_handler --monitor-self "
                            "--database=/home/alberto/.config/Hermes/Crashpad"),
                    "argv": [f"{self.GU}/chrome_crashpad_handler", "--monitor-self"],
                    "cwd": "/tmp", "state": "S"}
        self.assertEqual(self._scan({13: crashpad}, {13: []}), [])

    def test_two_independent_trees_yield_two_cards(self):
        procs = {11: self._gui(11, 1), 12: self._gui(12, 11, "--ozone-platform=wayland"),
                 21: self._gui(21, 1), 22: self._gui(22, 21, "--ozone-platform=wayland")}
        rows = self._scan(procs, {11: [12], 12: [], 21: [22], 22: []})
        self.assertEqual([r["pane_id"] for r in rows],
                         ["desktop:hermes:12", "desktop:hermes:22"])

    def test_empty_argv_gui_process_is_kept_fail_open(self):
        bare = {"pid": 31, "ppid": 1, "cmd": "/opt/Hermes", "argv": [],
                "cwd": "/tmp", "state": "S"}
        rows = self._scan({31: bare}, {31: []})
        self.assertEqual([r["pane_id"] for r in rows], ["desktop:hermes:31"])

    def test_live_capture_predicate_matches_only_launcher_and_app(self):
        with open(os.path.join(ROOT, "tests", "fixtures", "issue24-host-capture.json"),
                  encoding="utf-8") as fh:
            procs = json.load(fh)
        matched = [p["pid"] for p in procs
                   if ac.is_hermes_desktop_gui_process(p["cmd"], p["argv"])]
        # Launcher + app match at predicate level (the launcher is then
        # collapsed via its child in the scan); crashpad/zygote/utility must not.
        self.assertEqual(matched, [2878777, 2878805])

    def test_gui_child_walk_handles_paren_comm_vanished_pid_and_self(self):
        stats = {
            "/proc/10/stat": "10 (Hermes) S 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0\n",
            "/proc/11/stat": "11 (weird (name)) S 10 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0\n",
            "/proc/12/stat": "12 (other) S 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0\n",
            "/proc/14/stat": "garbage without parens\n",
        }

        def fake_open(path, mode="r", *args, **kwargs):
            if str(path) in stats:
                return io.StringIO(stats[str(path)])
            raise FileNotFoundError(path)

        with mock.patch.object(ac.glob, "glob",
                               return_value=["/proc/10", "/proc/11", "/proc/12",
                                             "/proc/14", "/proc/99"]), \
             mock.patch("builtins.open", side_effect=fake_open):
            # 11's comm holds spaces+parens yet resolves as child of 10;
            # vanished 99 and malformed 14 are skipped, never raised.
            self.assertEqual(ac.hermes_desktop_gui_child_pids(10), [11])
            self.assertEqual(ac.hermes_desktop_gui_child_pids(12), [])
            # Self-ppid entry can never suppress its own card.
            stats["/proc/10/stat"] = stats["/proc/10/stat"].replace(") S 1 ", ") S 10 ", 1)
            self.assertEqual(ac.hermes_desktop_gui_child_pids(10), [11])
