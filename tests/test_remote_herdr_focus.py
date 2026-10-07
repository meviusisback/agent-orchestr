#!/usr/bin/env python3
"""Hermetic tests for remote Herdr card focus and SSH multiplexing."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent_ctl as ac  # noqa: E402

MACHINE = {"id": "m1", "target": "user@devbox", "session": "default"}


class TestRemoteHerdrFocus(unittest.TestCase):
    def test_rejects_option_like_or_malformed_ids(self):
        for target in (
            "herdr-remote:m1:default|--evil",
            "herdr-remote:m1:bad session|p1",
            "herdr-remote:m1:default",
        ):
            with mock.patch.object(ac.subprocess, "Popen") as popen:
                result = ac.focus_pane(target)
            self.assertFalse(result["ok"], target)
            popen.assert_not_called()

    def test_unknown_machine_spawns_nothing(self):
        with mock.patch.object(ac, "herdr_machine_list", return_value=[]), \
             mock.patch.object(ac.subprocess, "Popen") as popen:
            result = ac.focus_pane("herdr-remote:m1:default|p1")
        self.assertFalse(result["ok"])
        popen.assert_not_called()

    def test_focuses_existing_attach_window(self):
        win = {"title": "herdr", "workspace": {"id": 3}}
        with mock.patch.object(ac, "herdr_machine_list", return_value=[MACHINE]), \
             mock.patch.object(ac, "find_herdr_remote_window", return_value=win), \
             mock.patch.object(ac, "focus_hypr_window") as focus, \
             mock.patch.object(ac.subprocess, "Popen") as popen:
            result = ac.focus_pane("herdr-remote:m1:default|p1")
        self.assertTrue(result["ok"])
        focus.assert_called_once_with(win)
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0], ["herdr", "--machine", "m1", "agent", "focus", "p1"])

    def test_launches_attach_when_no_window(self):
        with mock.patch.object(ac, "herdr_machine_list", return_value=[MACHINE]), \
             mock.patch.object(ac, "find_herdr_remote_window", return_value=None), \
             mock.patch.object(ac.subprocess, "Popen") as popen:
            result = ac.focus_pane("herdr-remote:m1:default|p1")
        self.assertTrue(result["ok"])
        self.assertEqual(
            popen.call_args_list[-1][0][0],
            ["omarchy-launch-terminal", "herdr", "--remote", "user@devbox", "--session", "default"],
        )


class TestSshMultiplexing(unittest.TestCase):
    def test_control_socket_only_in_runtime_dir(self):
        with tempfile.TemporaryDirectory() as runtime:
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
                argv = ac.remote_herdr_command(MACHINE)
            self.assertIn(f"ControlPath={os.path.join(runtime, 'herdr-plugin-ssh-%C')}", argv)

    def test_no_multiplexing_without_runtime_dir(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            argv = ac.remote_herdr_command(MACHINE)
        self.assertFalse(any(a.startswith("ControlPath=") for a in argv))
        self.assertNotIn("ControlMaster=auto", argv)


if __name__ == "__main__":
    unittest.main()
