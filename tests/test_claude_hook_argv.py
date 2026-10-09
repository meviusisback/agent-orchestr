#!/usr/bin/env python3
"""Hancore block (Verify #10717): hooks/claude-code-status.sh must not pass the
unredacted Notification message or session cwd via jq --arg (world-readable argv).

Hermetic: fake HOME (isolated STATE_DIR), stub jq first in PATH logging its argv,
and a real `exec -a claude` ancestor so find_claude_pid resolves. HOOK_SCRIPT env
overrides the hook path (used to prove the pre-fix script fails this test).
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.environ.get(
    "HOOK_SCRIPT", os.path.join(ROOT, "hooks", "claude-code-status.sh"))

LAUNCH_SH = """#!/bin/bash
# Runs the hook as a (foreground, stdin-sharing) child while THIS process
# carries argv0 "claude", so the hook's /proc ancestry walk resolves a
# claude PID. Foreground on purpose: background jobs lose stdin.
bash "$1"
"""

JQ_STUB = """#!/bin/bash
printf '%%s\\0' "$@" >> "$JQ_ARGV_LOG"
exec "%s" "$@"
"""


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class TestClaudeHookArgvHygiene(unittest.TestCase):
    def _run_hook(self, notification):
        tmp = tempfile.mkdtemp(prefix="hookargv-")
        self.addCleanup(shutil.rmtree, tmp, True)
        fake_home = os.path.join(tmp, "home")
        stubbin = os.path.join(tmp, "bin")
        os.makedirs(os.path.join(fake_home, ".local", "state"), exist_ok=True)
        os.makedirs(stubbin, exist_ok=True)
        real_jq = shutil.which("jq")
        stub = os.path.join(stubbin, "jq")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(JQ_STUB % real_jq)
        os.chmod(stub, 0o755)
        launch = os.path.join(tmp, "launch.sh")
        with open(launch, "w", encoding="utf-8") as fh:
            fh.write(LAUNCH_SH)
        os.chmod(launch, 0o755)
        argv_log = os.path.join(tmp, "jq-argv.log")
        env = dict(os.environ)
        env["PATH"] = stubbin + os.pathsep + env.get("PATH", "/usr/bin:/bin")
        env["HOME"] = fake_home
        env["JQ_ARGV_LOG"] = argv_log
        proc = subprocess.run(
            ["bash", "-c", 'exec -a claude bash "$1" "$2"', "_", launch, HOOK],
            input=json.dumps(notification).encode("utf-8"),
            env=env, timeout=30, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-500:])
        state_dir = os.path.join(
            fake_home, ".local", "state", "omarchy", "agents", "claude-status")
        files = [f for f in os.listdir(state_dir) if f.endswith(".json")]
        self.assertEqual(len(files), 1)
        with open(os.path.join(state_dir, files[0]), encoding="utf-8") as fh:
            written = json.load(fh)
        with open(argv_log, "rb") as fh:
            logged_argv = fh.read().decode("utf-8", errors="replace")
        return written, logged_argv

    def test_notification_secrets_never_reach_jq_argv(self):
        canary_msg = "canary-msg-auth-token-sk-test-987654321"
        canary_cwd = "/home/user/secret-project-canary"
        written, logged_argv = self._run_hook({
            "hook_event_name": "Notification",
            "cwd": canary_cwd,
            "message": canary_msg,
        })
        # Payload intact through the fd-based path.
        self.assertEqual(written["status"], "waiting")
        self.assertEqual(written["detail"], canary_msg)
        self.assertEqual(written["cwd"], canary_cwd)
        self.assertTrue(isinstance(written["updated"], int))
        # ...but invisible in every observed jq argv.
        self.assertNotIn(canary_msg, logged_argv)
        self.assertNotIn(canary_cwd, logged_argv)


if __name__ == "__main__":
    unittest.main()
