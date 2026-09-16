#!/usr/bin/env python3
"""Hermetic tests for remote source parsing and identity boundaries."""

import importlib.util
import json
import os
import subprocess
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("agent_ctl_under_test", os.path.join(ROOT, "agent_ctl.py"))
assert spec and spec.loader
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)


class TestHerdrMachineConfig(unittest.TestCase):
    def test_enabled_saved_machine_becomes_remote_source(self):
        machines = [
            {"id": "m1", "label": "Dev", "target": "dev", "session": "default", "enabled": True},
            {"id": "m2", "label": "Off", "target": "off", "session": "default", "enabled": False},
        ]
        self.assertEqual(ac.parse_herdr_machine_list(machines), [
            {"id": "m1", "label": "Dev", "target": "dev", "session": "default"}
        ])

    def test_remote_command_rejects_shell_metacharacters(self):
        with self.assertRaises(ValueError):
            ac.remote_herdr_command({"target": "dev; touch /tmp/pwned", "session": "default"})

    def test_remote_command_accepts_ssh_uri_and_discovers_binary(self):
        args = ac.remote_herdr_command({"target": "ssh://user@example.test:2222", "session": "default"})
        self.assertIn("-p", args)
        self.assertIn("user@example.test", args)
        self.assertIn("command -v herdr", args[-1])

    def test_remote_target_identity_is_machine_qualified(self):
        self.assertEqual(ac.remote_herdr_target_id("m1", "default", "p7"), "herdr-remote:m1:default|p7")

    def test_remote_control_targets_are_rejected(self):
        target = "herdr-remote:m1:default|w1:p1"
        self.assertFalse(ac.focus_pane(target)["ok"])
        self.assertFalse(ac.kill_target(target)["ok"])


class TestHermesRegistrySafety(unittest.TestCase):
    def test_registry_rows_never_expose_secret_or_token(self):
        registry = {
            "connections": [
                {"id": "r1", "kind": "remote", "label": "Infra", "url": "http://host:9120", "authMode": "oauth", "token": {"value": "secret"}},
                {"id": "local", "kind": "local", "label": "Local"},
            ]
        }
        rows = ac.sanitize_hermes_registry_connections(registry)
        self.assertEqual(rows[0], {"id": "r1", "kind": "remote", "label": "Infra", "url": "http://host:9120", "auth_mode": "oauth"})
        self.assertNotIn("token", rows[0])


class TestRemoteSnapshotTransport(unittest.TestCase):
    def test_snapshot_uses_machine_and_session_identity(self):
        fake = json.dumps({"id": "x", "result": {"snapshot": {"workspaces": [], "tabs": [], "panes": [], "agents": []}}})
        with mock.patch.object(ac, "run_bounded_remote_command", return_value=fake) as run:
            result = ac.query_remote_herdr_snapshot({"id": "m1", "target": "dev", "session": "agents"})
        self.assertEqual(result["result"]["snapshot"]["agents"], [])
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ["ssh", "-o", "BatchMode=yes", "-o"])
        self.assertIn('--session agents api snapshot', args[-1])
        self.assertIn('command -v herdr', args[-1])

    def test_remote_agent_read_uses_bounded_detection_command(self):
        with mock.patch.object(ac, "run_bounded_remote_command", return_value="Ready for prompt") as run:
            self.assertEqual(
                ac.query_remote_herdr_agent_read({"target": "dev", "session": "agents"}, "w1:p1"),
                "Ready for prompt",
            )
        self.assertIn("agent read w1:p1 --source detection", run.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()
