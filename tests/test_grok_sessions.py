#!/usr/bin/env python3
"""Fixture tests for Grok Build session discovery in agent_ctl.py.

Hermetic by construction: every case points GROK_HOME at a fresh temporary
directory, so the suite never reads the real ~/.grok, never touches the network,
hyprctl, Orca or Herdr, and never calls fetch_all_agents(). Only the pure Grok
helpers run here, which keeps this safe to execute on a live desktop.

Run:  python3 tests/test_grok_sessions.py
"""

import importlib.util
import json
import os
import shutil
import signal
import sys
import tempfile
import unittest
import uuid
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

# Long path whose URL-encoded form far exceeds the 255-byte group-name limit,
# i.e. the case Grok stores as a slug+hash group with a `.cwd` marker file.
LONG_CWD = "/home/agent/very/deep/" + "/".join("segment-%02d-abcdefghijklmnopqrstuvwxyz" % i for i in range(12))


class GrokFixtureCase(unittest.TestCase):
    """Common fixture helpers: temp GROK_HOME, session/event writers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="grok-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"GROK_HOME": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        ac._GROK_BUDGET.reset()

    # -- writers -----------------------------------------------------------
    def make_session(self, group, session_id, summary=None, events=None, history=None, cwd_marker=None):
        group_dir = os.path.join(self.tmp, "sessions", group)
        session_dir = os.path.join(group_dir, session_id)
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary if summary is not None else {"current_model_id": "grok-4.6", "generated_title": "Fix the build"}, f)
        if events is not None:
            with open(os.path.join(session_dir, "events.jsonl"), "w", encoding="utf-8") as f:
                for entry in events:
                    f.write(json.dumps(entry) + "\n")
        if history is not None:
            with open(os.path.join(session_dir, "chat_history.jsonl"), "w", encoding="utf-8") as f:
                for entry in history:
                    f.write(json.dumps(entry) + "\n")
        if cwd_marker is not None:
            with open(os.path.join(group_dir, ".cwd"), "w", encoding="utf-8") as f:
                f.write(cwd_marker)
        return session_dir

    def write_roster(self, rows):
        with open(os.path.join(self.tmp, "active_sessions.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f)


class TestInvocationClassification(GrokFixtureCase):
    def test_cli_verbs_are_not_sessions(self):
        for verb in ac.GROK_HELPER_SUBCOMMANDS:
            self.assertTrue(ac.is_grok_helper_process("grok %s" % verb, ["grok", verb]), verb)

    def test_interactive_forms_are_sessions(self):
        cases = [
            ["grok"],
            ["grok", "--resume"],
            ["grok", "--resume", "some-title"],
            ["grok", "--session-id", str(uuid.uuid4())],
            ["grok", "fix the failing test"],
            ["grok", "agent loop rewrite"],  # prompt text, not the `agent` verb
        ]
        for argv in cases:
            self.assertFalse(ac.is_grok_helper_process(" ".join(argv), argv), argv)

    def test_headless_and_info_invocations_are_not_sessions(self):
        cases = [
            ["grok", "-p", "hi"],
            ["grok", "--single", "hi"],
            ["grok", "--prompt-file", "/tmp/p.txt"],
            ["grok", "--prompt-json", "[]"],
            ["grok", "--output-format", "json", "hi"],
            ["grok", "--json-schema", "{}"],
            ["grok", "-v"],
            ["grok", "--version"],
            ["grok", "-h"],
            ["grok", "--help"],
            ["grok", "--show-current"],
        ]
        for argv in cases:
            self.assertTrue(ac.is_grok_helper_process(" ".join(argv), argv), argv)

    def test_headless_flag_late_in_argv_is_still_detected(self):
        argv = ["grok", "--model", "grok-4.6", "--cwd", "/tmp", "-p", "hi"]
        self.assertTrue(ac.is_grok_helper_process(" ".join(argv), argv))

    def test_other_binaries_are_ignored(self):
        self.assertFalse(ac.is_grok_helper_process("claude daemon", ["claude", "daemon"]))
        self.assertFalse(ac.is_grok_helper_process("node /usr/bin/grok", ["node", "/usr/bin/grok"]))

    def test_cwd_override_detection(self):
        self.assertTrue(ac.grok_argv_has_cwd_override(["grok", "--cwd", "/tmp"]))
        self.assertTrue(ac.grok_argv_has_cwd_override(["grok", "--cwd=/tmp"]))
        self.assertFalse(ac.grok_argv_has_cwd_override(["grok", "--resume"]))


class TestSessionIdAndTraversal(GrokFixtureCase):
    def test_validator_rejects_traversal_and_junk(self):
        bad = ["", "..", "../..", "/etc/passwd", "abc", "%2e%2e", "..%2f..", "....", "x" * 8, "a" * 10000, "id\n"]
        for value in bad:
            self.assertIsNone(ac.GROK_SESSION_ID_RE.match(value), value)

    def test_validator_accepts_uuid_shape(self):
        self.assertIsNotNone(ac.GROK_SESSION_ID_RE.match(str(uuid.uuid4())))

    def test_traversal_id_resolves_nothing(self):
        outside = tempfile.mkdtemp(prefix="grok-outside-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with open(os.path.join(outside, "summary.json"), "w", encoding="utf-8") as f:
            json.dump({"generated_title": "secret"}, f)
        os.makedirs(os.path.join(self.tmp, "sessions"), exist_ok=True)
        self.assertIsNone(ac.grok_session_dir_for_id("/tmp", "../../" + os.path.basename(outside)))
        self.assertIsNone(ac.grok_session_dir_for_id("/tmp", "/etc"))


class TestRosterResolution(GrokFixtureCase):
    def test_pid_maps_to_session_without_matching_the_group_name(self):
        """The session id is the key, so a slug+hash group still resolves."""
        sid = str(uuid.uuid4())
        session_dir = self.make_session("slug-8f2a1b3c", sid, summary={"current_model_id": "grok-4.6"})
        self.write_roster([{"session_id": sid, "pid": os.getpid(), "cwd": "/home/agent/project", "opened_at": "2026-09-11T10:00:00Z"}])
        self.assertEqual(ac.grok_active_session_for_pid(os.getpid()), session_dir)

    def test_encoded_group_fast_path(self):
        sid = str(uuid.uuid4())
        cwd = "/home/agent/project"
        session_dir = self.make_session(ac.quote(cwd, safe=""), sid)
        self.write_roster([{"session_id": sid, "pid": os.getpid(), "cwd": cwd, "opened_at": "x"}])
        self.assertEqual(ac.grok_active_session_for_pid(os.getpid()), session_dir)

    def test_other_pid_is_not_matched(self):
        sid = str(uuid.uuid4())
        self.make_session("group", sid)
        self.write_roster([{"session_id": sid, "pid": 999999, "cwd": "/tmp", "opened_at": "x"}])
        self.assertIsNone(ac.grok_active_session_for_pid(os.getpid()))

    def test_malformed_roster_is_ignored(self):
        with open(os.path.join(self.tmp, "active_sessions.json"), "w", encoding="utf-8") as f:
            f.write('{"session_id": "truncated')
        self.assertIsNone(ac.grok_active_session_for_pid(os.getpid()))

    def test_roster_rows_with_traversal_ids_are_dropped(self):
        self.make_session("group", str(uuid.uuid4()))
        self.write_roster([{"session_id": "../../etc", "pid": os.getpid(), "cwd": "/tmp", "opened_at": "x"}])
        self.assertIsNone(ac.grok_active_session_for_pid(os.getpid()))

    def test_oversized_roster_is_refused(self):
        big = [{"session_id": str(uuid.uuid4()), "pid": os.getpid(), "cwd": "/tmp", "opened_at": "x" * 4096} for _ in range(128)]
        self.write_roster(big)
        self.assertGreater(os.path.getsize(os.path.join(self.tmp, "active_sessions.json")), ac.GROK_ROSTER_MAX_BYTES)
        self.assertIsNone(ac.grok_active_session_for_pid(os.getpid()))

    def test_subagent_session_is_skipped(self):
        sid = str(uuid.uuid4())
        self.make_session("group", sid, summary={"current_model_id": "grok-4.6", "session_kind": "subagent:explorer"})
        self.write_roster([{"session_id": sid, "pid": os.getpid(), "cwd": "/tmp", "opened_at": "x"}])
        self.assertIsNone(ac.grok_active_session_for_pid(os.getpid()))


class TestPathIntegrity(GrokFixtureCase):
    def test_symlinked_session_dir_is_not_trusted(self):
        outside = tempfile.mkdtemp(prefix="grok-outside-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        sid = str(uuid.uuid4())
        with open(os.path.join(outside, "summary.json"), "w", encoding="utf-8") as f:
            json.dump({"generated_title": "outside"}, f)
        group = os.path.join(self.tmp, "sessions", "group")
        os.makedirs(group, exist_ok=True)
        os.symlink(outside, os.path.join(group, sid))
        self.write_roster([{"session_id": sid, "pid": os.getpid(), "cwd": "/tmp", "opened_at": "x"}])
        self.assertFalse(ac.grok_trusted_path(os.path.join(group, sid)))
        self.assertIsNone(ac.grok_active_session_for_pid(os.getpid()))
        self.assertEqual(ac.extract_grok_task_from_session(os.path.join(group, sid)), (None, None, None, None, False))

    def test_symlinked_summary_file_is_not_read(self):
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid, summary={"generated_title": "real"})
        outside = tempfile.mkdtemp(prefix="grok-outside-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        decoy = os.path.join(outside, "summary.json")
        with open(decoy, "w", encoding="utf-8") as f:
            json.dump({"generated_title": "decoy"}, f)
        os.remove(os.path.join(session_dir, "summary.json"))
        os.symlink(decoy, os.path.join(session_dir, "summary.json"))
        self.assertIsNone(ac.grok_read_json(os.path.join(session_dir, "summary.json"), ac.GROK_SUMMARY_MAX_BYTES))

    def test_path_outside_grok_home_is_not_trusted(self):
        outside = tempfile.mkdtemp(prefix="grok-outside-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        self.assertFalse(ac.grok_trusted_path(outside))

    def test_group_world_writable_is_not_trusted(self):
        sid = str(uuid.uuid4())
        group = os.path.join(self.tmp, "sessions", "group")
        self.make_session("group", sid)
        os.chmod(group, 0o777)
        self.addCleanup(os.chmod, group, 0o755)
        self.assertFalse(ac.grok_trusted_path(os.path.join(group, sid)))


class TestCwdResolution(GrokFixtureCase):
    def test_encoded_group_finds_session(self):
        cwd = "/home/agent/project"
        sid = str(uuid.uuid4())
        session_dir = self.make_session(ac.quote(cwd, safe=""), sid)
        self.assertEqual(ac.grok_sessions_for_cwd(cwd), [session_dir])

    def test_long_cwd_group_is_found_via_cwd_marker(self):
        sid = str(uuid.uuid4())
        session_dir = self.make_session("slug-abc123-deadbeef", sid, cwd_marker=LONG_CWD)
        self.assertGreater(len(ac.quote(LONG_CWD, safe="")), 255)
        self.assertEqual(ac.grok_sessions_for_cwd(LONG_CWD), [session_dir])

    def test_unrelated_group_is_not_matched(self):
        cwd = "/home/agent/project"
        sid = str(uuid.uuid4())
        self.make_session("some-other-group", sid, cwd_marker="/elsewhere")
        self.assertEqual(ac.grok_sessions_for_cwd(cwd), [])

    def test_newest_session_first(self):
        cwd = "/home/agent/project"
        group = ac.quote(cwd, safe="")
        older = self.make_session(group, str(uuid.uuid4()))
        newer = self.make_session(group, str(uuid.uuid4()))
        os.utime(os.path.join(older, "summary.json"), (1000, 1000))
        os.utime(os.path.join(newer, "summary.json"), (2000, 2000))
        self.assertEqual(ac.grok_sessions_for_cwd(cwd), [newer, older])

    def test_subagent_session_is_not_listed(self):
        cwd = "/home/agent/project"
        parent = self.make_session(ac.quote(cwd, safe=""), str(uuid.uuid4()), summary={"generated_title": "parent"})
        self.make_session(ac.quote(cwd, safe=""), str(uuid.uuid4()), summary={"session_kind": "subagent:x"})
        self.assertEqual(ac.grok_sessions_for_cwd(cwd), [parent])

    def test_group_cwd_marker_read(self):
        sid = str(uuid.uuid4())
        self.make_session("slug-hash", sid, cwd_marker=LONG_CWD)
        self.assertEqual(ac.grok_group_cwd(os.path.join(self.tmp, "sessions", "slug-hash")), LONG_CWD)
        encoded = "/home/agent/project"
        self.make_session(ac.quote(encoded, safe=""), str(uuid.uuid4()))
        self.assertEqual(ac.grok_group_cwd(os.path.join(self.tmp, "sessions", ac.quote(encoded, safe=""))), encoded)


class TestStatusMapping(GrokFixtureCase):
    def extract(self, events=None, summary=None, history=None):
        session_dir = self.make_session("group", str(uuid.uuid4()), summary=summary, events=events, history=history)
        return ac.extract_grok_task_from_session(session_dir)

    def test_permission_prompt_is_waiting(self):
        prompt, detail, model, status, has_q = self.extract(events=[{"type": "phase_changed", "phase": "permission_prompt"}])
        self.assertEqual(status, "waiting")
        self.assertTrue(has_q)
        self.assertIn("permission", detail.lower())

    def test_permission_requested_is_waiting_with_tool(self):
        _, detail, _, status, has_q = self.extract(events=[{"type": "permission_requested", "tool_name": "bash"}])
        self.assertEqual(status, "waiting")
        self.assertTrue(has_q)
        self.assertIn("bash", detail)

    def test_tool_execution_is_working(self):
        _, detail, _, status, _ = self.extract(events=[{"type": "tool_started", "tool_name": "grep"}, {"type": "tool_completed", "tool_name": "grep"}, {"type": "phase_changed", "phase": "tool_execution"}])
        self.assertEqual(status, "working")

    def test_waiting_for_model_is_working(self):
        _, detail, _, status, _ = self.extract(events=[{"type": "turn_started"}, {"type": "phase_changed", "phase": "waiting_for_model"}])
        self.assertEqual(status, "working")
        self.assertEqual(detail, "Thinking…")

    def test_turn_ended_is_completed_with_turn_summary(self):
        session_dir = self.make_session(
            "group",
            str(uuid.uuid4()),
            summary={"current_model_id": "grok-4.6", "generated_title": "Fix build", "last_turn_summary": "Short answer given"},
            events=[{"type": "turn_started"}, {"type": "turn_ended", "outcome": "completed"}],
        )
        _, detail, _, status, _ = ac.extract_grok_task_from_session(session_dir)
        self.assertEqual(status, "completed")
        self.assertEqual(detail, "Short answer given")

    def test_no_events_is_idle(self):
        _, detail, _, status, _ = self.extract()
        self.assertEqual(status, "idle")
        self.assertEqual(detail, "Ready for prompt")

    def test_latest_user_prompt_wins_over_title(self):
        prompt, _, _, _, _ = self.extract(
            summary={"generated_title": "auto title"},
            history=[{"type": "user", "content": "please fix the failing test"}, {"type": "assistant", "content": "ok"}],
        )
        self.assertEqual(prompt, "please fix the failing test")

    def test_synthetic_and_secret_bearing_entries(self):
        prompt, _, _, _, _ = self.extract(
            summary={"generated_title": "auto title"},
            history=[
                {"type": "user", "content": "leak sk-abcdefghijklmnopqrstuvwx here"},
                {"type": "user", "content": "synthetic", "synthetic_reason": "auto_continue"},
            ],
        )
        self.assertIn("REDACTED", prompt)
        self.assertNotIn("abcdefghijklmnopqrstuvwx", prompt)

    def test_model_and_title_from_summary(self):
        prompt, _, model, _, _ = self.extract(summary={"current_model_id": "xai/grok-4.6", "generated_title": "Rename helper"})
        self.assertEqual(model, "grok-4.6")
        self.assertEqual(prompt, "Rename helper")

    def test_oversized_summary_is_refused_but_events_still_read(self):
        session_dir = self.make_session("group", str(uuid.uuid4()), events=[{"type": "turn_ended"}])
        with open(os.path.join(session_dir, "summary.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"generated_title": "x" * (ac.GROK_SUMMARY_MAX_BYTES + 16)}))
        _, _, model, status, _ = ac.extract_grok_task_from_session(session_dir)
        self.assertEqual(model, "")
        self.assertEqual(status, "completed")


class TestBudget(GrokFixtureCase):
    def test_budget_stops_reads_and_listings(self):
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid)
        ac._GROK_BUDGET.spend(ac.GROK_CYCLE_MAX_BYTES + 1)
        self.assertTrue(ac._GROK_BUDGET.exhausted())
        self.assertIsNone(ac.grok_read_json(os.path.join(session_dir, "summary.json"), ac.GROK_SUMMARY_MAX_BYTES))
        self.assertEqual(ac.grok_listdir(os.path.join(self.tmp, "sessions")), [])
        self.assertFalse(ac.grok_trusted_path(session_dir))

    def test_cycle_reset_restores_reads(self):
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid)
        ac._GROK_BUDGET.spend(ac.GROK_CYCLE_MAX_BYTES + 1)
        ac._GROK_BUDGET.reset()
        self.assertEqual(ac.grok_read_json(os.path.join(session_dir, "summary.json"), ac.GROK_SUMMARY_MAX_BYTES)["current_model_id"], "grok-4.6")


class TestHostileFiles(GrokFixtureCase):
    """Files that would otherwise hang, mislead or leak must degrade safely."""

    def _with_timeout(self, seconds, fn):
        """Run fn with an alarm so a blocking regression fails instead of hanging."""
        def _raise(signum, frame):
            raise AssertionError("call blocked for more than %ss" % seconds)

        previous = signal.signal(signal.SIGALRM, _raise)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return fn()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def test_fifo_instead_of_roster_does_not_block(self):
        os.mkfifo(os.path.join(self.tmp, "active_sessions.json"))
        self.assertIsNone(self._with_timeout(5, lambda: ac.grok_active_session_for_pid(os.getpid())))

    def test_fifo_instead_of_summary_does_not_block(self):
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid)
        os.remove(os.path.join(session_dir, "summary.json"))
        os.mkfifo(os.path.join(session_dir, "summary.json"))
        self.assertIsNone(self._with_timeout(5, lambda: ac.grok_read_json(os.path.join(session_dir, "summary.json"), ac.GROK_SUMMARY_MAX_BYTES)))
        # The session degrades to a bare idle card rather than blocking or raising.
        self.assertEqual(self._with_timeout(5, lambda: ac.extract_grok_task_from_session(session_dir)), (None, "Ready for prompt", "", "idle", False))

    def test_fifo_instead_of_events_does_not_block(self):
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid)
        os.mkfifo(os.path.join(session_dir, "events.jsonl"))
        _, _, _, status, _ = self._with_timeout(5, lambda: ac.extract_grok_task_from_session(session_dir))
        self.assertEqual(status, "idle")

    def test_fifo_cwd_marker_does_not_block(self):
        group = os.path.join(self.tmp, "sessions", "g")
        os.makedirs(group, exist_ok=True)
        os.mkfifo(os.path.join(group, ".cwd"))
        self.assertIsNone(self._with_timeout(5, lambda: ac.grok_group_cwd_marker(group)))

    def test_hardlinked_summary_is_refused(self):
        outside = tempfile.mkdtemp(prefix="grok-outside-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        planted = os.path.join(outside, "secret.json")
        with open(planted, "w", encoding="utf-8") as f:
            json.dump({"generated_title": "HARDLINKED"}, f)
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid)
        os.remove(os.path.join(session_dir, "summary.json"))
        os.link(planted, os.path.join(session_dir, "summary.json"))
        self.assertFalse(ac.grok_is_regular_file(os.path.join(session_dir, "summary.json")))
        self.assertIsNone(ac.grok_read_json(os.path.join(session_dir, "summary.json"), ac.GROK_SUMMARY_MAX_BYTES))
        prompt, _, _, _, _ = ac.extract_grok_task_from_session(session_dir)
        self.assertNotEqual(prompt, "HARDLINKED")

    def test_long_but_valid_event_is_not_dropped(self):
        """The newest event decides the status, so a large one must still parse."""
        sid = str(uuid.uuid4())
        events = [{"type": "turn_started"}, {"type": "permission_requested", "tool_name": "bash", "payload": "y" * 20000}]
        session_dir = self.make_session("group", sid, events=events)
        _, detail, _, status, has_q = ac.extract_grok_task_from_session(session_dir)
        self.assertEqual(status, "waiting")
        self.assertTrue(has_q)
        self.assertIn("bash", detail)

    def test_secret_in_tool_name_is_redacted(self):
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid, events=[{"type": "tool_started", "tool_name": "sk-abcdefghijklmnopqrstuvwx"}])
        _, detail, _, status, _ = ac.extract_grok_task_from_session(session_dir)
        self.assertEqual(status, "working")
        self.assertIn("REDACTED", detail)
        self.assertNotIn("abcdefghijklmnopqrstuvwx", detail)

    def test_secret_in_permission_tool_name_is_redacted(self):
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid, events=[{"type": "permission_requested", "tool_name": "ghp_abcdEFGHIJKLMNOPQRSTUV"}])
        _, detail, _, status, _ = ac.extract_grok_task_from_session(session_dir)
        self.assertEqual(status, "waiting")
        self.assertIn("REDACTED", detail)

    def test_dot_cwd_does_not_escape_into_grok_home(self):
        """quote() leaves '.' alone, so a roster cwd of '..' must be refused."""
        sid = str(uuid.uuid4())
        planted = os.path.join(self.tmp, sid)
        os.makedirs(planted, exist_ok=True)
        with open(os.path.join(planted, "summary.json"), "w", encoding="utf-8") as f:
            json.dump({"generated_title": "ABOVE SESSIONS"}, f)
        self.assertIsNone(ac.grok_session_dir_for_id("..", sid))
        self.assertIsNone(ac.grok_session_dir_for_id(".", sid))

    def test_trailing_newline_session_id_is_rejected(self):
        self.assertIsNone(ac.GROK_SESSION_ID_RE.match(str(uuid.uuid4()) + "\n"))
        self.assertIsNotNone(ac.GROK_SESSION_ID_RE.match(str(uuid.uuid4())))


class TestEventOrdering(GrokFixtureCase):
    """Event order decides the status: the newest event must win."""

    def state(self, events):
        session_dir = self.make_session("group", str(uuid.uuid4()), events=events)
        _, detail, _, status, has_q = ac.extract_grok_task_from_session(session_dir)
        return status, detail, has_q

    def test_tool_started_after_permission_prompt_is_working(self):
        status, detail, _ = self.state([{"type": "phase_changed", "phase": "permission_prompt"}, {"type": "tool_started", "tool_name": "bash"}])
        self.assertEqual(status, "working")
        self.assertIn("bash", detail)

    def test_permission_resolved_clears_the_stale_phase(self):
        status, _, _ = self.state([
            {"type": "turn_started"},
            {"type": "phase_changed", "phase": "permission_prompt"},
            {"type": "permission_requested", "tool_name": "bash"},
            {"type": "permission_resolved", "tool_name": "bash"},
        ])
        self.assertEqual(status, "working")

    def test_permission_prompt_after_resolve_still_waits(self):
        status, _, has_q = self.state([
            {"type": "permission_requested", "tool_name": "bash"},
            {"type": "permission_resolved", "tool_name": "bash"},
            {"type": "phase_changed", "phase": "permission_prompt"},
        ])
        self.assertEqual(status, "waiting")
        self.assertTrue(has_q)

    def test_turn_completed_after_tools_is_completed(self):
        status, detail, _ = self.state([
            {"type": "turn_started"},
            {"type": "tool_started", "tool_name": "grep"},
            {"type": "tool_completed", "tool_name": "grep"},
            {"type": "phase_changed", "phase": "streaming_text"},
            {"type": "turn_ended", "outcome": "completed"},
        ])
        self.assertEqual(status, "completed")

    def test_partial_tail_starting_mid_line_still_parses(self):
        """A tail that starts inside a line must drop only that partial line."""
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid)
        filler = json.dumps({"type": "phase_changed", "phase": "streaming_text", "pad": "x" * 200}) + "\n"
        with open(os.path.join(session_dir, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write(filler * 400)
            f.write(json.dumps({"type": "turn_ended"}) + "\n")
        _, _, _, status, _ = ac.extract_grok_task_from_session(session_dir)
        self.assertEqual(status, "completed")


class TestGroupCwdMarker(GrokFixtureCase):
    def test_marker_is_optional(self):
        sid = str(uuid.uuid4())
        self.make_session("plain-group", sid)
        group = os.path.join(self.tmp, "sessions", "plain-group")
        self.assertIsNone(ac.grok_group_cwd_marker(group))
        self.assertEqual(ac.grok_group_cwd(group), "plain-group")

    def test_marker_wins_over_group_name(self):
        sid = str(uuid.uuid4())
        self.make_session("slug-hash", sid, cwd_marker=LONG_CWD)
        group = os.path.join(self.tmp, "sessions", "slug-hash")
        self.assertEqual(ac.grok_group_cwd_marker(group), LONG_CWD)
        self.assertEqual(ac.grok_group_cwd(group), LONG_CWD)


class TestMarkerAndBudget(GrokFixtureCase):
    """The `.cwd` marker is untrusted text and must not become UI text or cost."""

    def test_relative_marker_text_is_rejected(self):
        self.make_session("slug", str(uuid.uuid4()), cwd_marker="not/a/path\nINJECT")
        group = os.path.join(self.tmp, "sessions", "slug")
        self.assertIsNone(ac.grok_group_cwd_marker(group))

    def test_marker_with_ansi_and_secret_is_cleaned(self):
        self.make_session("slug", str(uuid.uuid4()), cwd_marker="/tmp/proj\x1b[31m sk-abcdefghijklmnopqrstuvwx\x1b[0m")
        group = os.path.join(self.tmp, "sessions", "slug")
        marker = ac.grok_group_cwd_marker(group)
        self.assertIsNotNone(marker)
        self.assertNotIn("\x1b", marker)
        self.assertIn("REDACTED", marker)
        self.assertNotIn("abcdefghijklmnopqrstuvwx", marker)

    def test_marker_read_is_memoized_per_cycle(self):
        self.make_session("slug", str(uuid.uuid4()), cwd_marker="/tmp/proj")
        group = os.path.join(self.tmp, "sessions", "slug")
        ac._GROK_BUDGET.reset()
        first = ac.grok_group_cwd_marker(group)
        ops_after_first = ac._GROK_BUDGET.ops
        second = ac.grok_group_cwd_marker(group)
        self.assertEqual(first, second)
        self.assertEqual(ac._GROK_BUDGET.ops, ops_after_first)

    def test_many_marker_groups_still_resolve_within_budget(self):
        """A 200-group marker-bearing tree must not exhaust the op budget."""
        target_cwd = "/home/agent/target-project"
        dirs = {}
        for i in range(200):
            cwd = "/home/agent/other-%03d" % i
            dirs[cwd] = self.make_session("slug-%03d" % i, str(uuid.uuid4()), cwd_marker=cwd)
        session_dir = self.make_session("slug-target", str(uuid.uuid4()), cwd_marker=target_cwd)
        ac._GROK_BUDGET.reset()
        found = ac.grok_sessions_for_cwd(target_cwd)
        self.assertIn(session_dir, found)
        self.assertFalse(ac._GROK_BUDGET.exhausted(), "op budget exhausted at %d ops" % ac._GROK_BUDGET.ops)
        # A second card in the same tick must still resolve.
        self.assertEqual(ac.grok_sessions_for_cwd("/home/agent/other-199"), [dirs["/home/agent/other-199"]])

    def test_roster_is_read_once_per_cycle(self):
        sid = str(uuid.uuid4())
        self.make_session("group", sid)
        self.write_roster([{"session_id": sid, "pid": os.getpid(), "cwd": "/tmp", "opened_at": "x"}])
        ac._GROK_BUDGET.reset()
        self.assertIsNotNone(ac.grok_active_session_for_pid(os.getpid()))
        bytes_after_first = ac._GROK_BUDGET.bytes_read
        self.assertIsNotNone(ac.grok_active_session_for_pid(os.getpid()))
        self.assertEqual(ac._GROK_BUDGET.bytes_read, bytes_after_first)

    def test_read_is_clamped_to_remaining_allowance(self):
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid)
        path = os.path.join(session_dir, "summary.json")
        ac._GROK_BUDGET.reset()
        ac._GROK_BUDGET.bytes_read = ac.GROK_CYCLE_MAX_BYTES - 5
        data = ac.grok_read_bytes(path, ac.GROK_SUMMARY_MAX_BYTES)
        self.assertIsNotNone(data)
        self.assertLessEqual(len(data), 5)
        self.assertLessEqual(ac._GROK_BUDGET.bytes_read, ac.GROK_CYCLE_MAX_BYTES + 1)


class TestLargeEventsAndStatusPayload(GrokFixtureCase):
    def test_single_huge_event_line_still_sets_status(self):
        """A decisive event larger than the 64 KiB window must not be lost."""
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid)
        with open(os.path.join(session_dir, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "taskId", "pad": "z" * 100000}) + "\n")
            f.write(json.dumps({"type": "permission_requested", "tool_name": "bash", "payload": "z" * 100000}) + "\n")
        _, detail, _, status, _ = ac.extract_grok_task_from_session(session_dir)
        self.assertEqual(status, "waiting")
        self.assertIn("bash", detail)

    def test_model_id_is_redacted(self):
        sid = str(uuid.uuid4())
        session_dir = self.make_session("group", sid, summary={"current_model_id": "xai/sk-abcdefghijklmnopqrstuvwx", "generated_title": "x"})
        _, _, model, _, _ = ac.extract_grok_task_from_session(session_dir)
        self.assertNotIn("abcdefghijklmnopqrstuvwx", model)
        self.assertIn("REDACTED", model)

    def test_status_payload_caps_agents(self):
        payload = {"ok": True, "connected": False, "summary": {"total": 300}, "agents": [{"pane_id": "x%d" % i, "title": "t"} for i in range(300)], "workspaces": []}
        text = ac.dump_status_json(payload)
        parsed = json.loads(text)
        self.assertEqual(len(parsed["agents"]), ac.STATUS_MAX_AGENTS)

    def test_status_payload_falls_back_to_valid_json(self):
        payload = {
            "ok": True,
            "connected": True,
            "summary": {"total": 1},
            "agents": [{"pane_id": "p", "title": "y" * 40000} for _ in range(256)],
            "workspaces": [],
        }
        text = ac.dump_status_json(payload)
        self.assertLessEqual(len(text), ac.STATUS_MAX_BYTES)
        parsed = json.loads(text)
        self.assertTrue(parsed.get("truncated"))
        self.assertEqual(parsed["agents"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
