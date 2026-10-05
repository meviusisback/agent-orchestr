#!/usr/bin/env python3
"""Hermetic tests for the remote Hermes peer source (issue #21).

Covers the parser, the confined-file reader, the key loader, the transport, the
status mapping, the card builder and the read-only refusals.

Every refusal test creates the hostile precondition DELIBERATELY (chmod after
creation, os.symlink, an injected uid) and asserts the loader did NOT run — a
test that passes for an incidental reason is worse than no test. Modes come from
os.chmod, never from the creation call, because umask masks the latter.

The dev session is a bwrap userns where root is unmapped, so root-owned
directories read as uid 65534. Production would see uid 0; the suite injects the
uid this sandbox actually shows (SANDBOX_ANCESTOR_UIDS) rather than weakening the
guard, and `_anchor_uids` is what a real host resolves to.

Run: python3 tests/test_peer_sources.py
"""

import importlib.util
import json
import os
import socket
import stat
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("agent_ctl_under_test", os.path.join(ROOT, "agent_ctl.py"))
assert spec and spec.loader
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)

# What root looks like from inside this sandbox's uid namespace.
SANDBOX_ANCESTOR_UIDS = (os.getuid(), 0, 65534)
PEER_KEY = "opaque-peer-key-0123456789abcdef"


def _fixture_home(testcase, tag="peer"):
    """A 0700 home with a .hermes inside it, on whatever base is writable.

    The real $HOME first (the confined reader validates the fixture's ANCESTORS,
    and $TMPDIR differs per shell — a suite green against world-writable /tmp
    would be red for the user). When $HOME is read-only (a bwrap sandbox, or a
    home on a locked volume) fall back to Hermes's private scratch: the fixture
    is still judged only up to itself, because trust_root anchors the walk.
    """
    for parent in (os.path.expanduser("~"), os.environ.get("TMPDIR") or "/tmp"):
        if not parent or not os.path.isdir(parent):
            continue
        base = os.path.join(parent, f".peer-test-{tag}-{os.getpid()}-{id(testcase):x}")
        try:
            os.makedirs(os.path.join(base, ".hermes"), exist_ok=True)
        except OSError:
            continue
        os.chmod(base, 0o700)
        os.chmod(os.path.join(base, ".hermes"), 0o700)
        return base, os.path.join(base, ".hermes")
    raise unittest.SkipTest("no writable base for the fixture")


class PeerConfinedReader(unittest.TestCase):
    """The credential file gets the strict recipe; config gets the bounded one."""

    def setUp(self):
        self.home, self.hermes = _fixture_home(self)
        self.addCleanup(self._cleanup)
        self.env_path = os.path.join(self.hermes, ".env")
        self._write(self.env_path, f"HERMES_PEER_SPARK_KEY={PEER_KEY}\n", 0o600)
        self.kwargs = dict(roots=[self.hermes], trust_root=self.home,
                           ancestor_uids=SANDBOX_ANCESTOR_UIDS, uid=os.getuid(), home=self.home)

    def _cleanup(self):
        for name in os.listdir(self.hermes):
            os.chmod(os.path.join(self.hermes, name), 0o600)
        for root, dirs, files in os.walk(self.home):
            for name in files:
                os.chmod(os.path.join(root, name), 0o600)
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    @staticmethod
    def _write(path, text, mode):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, mode)  # mode AFTER creation: umask would mask it otherwise

    def test_strict_profile_reads_a_0600_credential_file(self):
        self.assertEqual(ac.read_confined_text("env", self.env_path, **self.kwargs), f"HERMES_PEER_SPARK_KEY={PEER_KEY}\n")

    def test_absent_file_is_silent_not_an_error(self):
        # No .env at all is the common case (nobody registered a peer).
        self.assertIsNone(ac.read_confined_text("env", os.path.join(self.hermes, "nope.env"), **self.kwargs))

    def test_group_readable_credential_file_is_refused(self):
        os.chmod(self.env_path, 0o640)
        with mock.patch("sys.stderr") as err:
            self.assertIsNone(ac.read_confined_text("env", self.env_path, **self.kwargs))
        self.assertTrue(any("refused" in str(c) for c in err.write.call_args_list), "refusal must be stated")

    def test_world_readable_credential_file_is_refused(self):
        os.chmod(self.env_path, 0o604)
        with mock.patch("sys.stderr"):
            self.assertIsNone(ac.read_confined_text("env", self.env_path, **self.kwargs))

    def test_hard_linked_credential_file_is_refused(self):
        link = os.path.join(self.hermes, "alias.env")
        os.link(self.env_path, link)
        self.assertEqual(os.stat(self.env_path).st_nlink, 2)
        with mock.patch("sys.stderr"):
            self.assertIsNone(ac.read_confined_text("env", self.env_path, **self.kwargs))

    def test_symlink_at_the_exact_path_is_refused(self):
        link = os.path.join(self.hermes, "linked.env")
        os.symlink(self.env_path, link)
        with mock.patch("sys.stderr"):
            self.assertIsNone(ac.read_confined_text("env", link, **self.kwargs))

    def test_trailing_slash_does_not_walk_past_the_symlink_check(self):
        # islink("link/") is False for a symlink to a regular file, so the
        # normpath-before-islink order is what catches this.
        link = os.path.join(self.hermes, "slashy.env")
        os.symlink(self.env_path, link)
        with mock.patch("sys.stderr"):
            self.assertIsNone(ac.read_confined_text("env", link + "/", **self.kwargs))

    def test_fifo_does_not_wedge_the_read(self):
        # O_NONBLOCK is load-bearing: a FIFO passes every path check and would
        # block forever in open() without it.
        fifo = os.path.join(self.hermes, "pipe.env")
        os.mkfifo(fifo, 0o600)
        started = time.monotonic()
        with mock.patch("sys.stderr"):
            self.assertIsNone(ac.read_confined_text("env", fifo, **self.kwargs))
        self.assertLess(time.monotonic() - started, 2.0, "the open() hung on a FIFO")

    def test_oversized_file_is_refused(self):
        self._write(self.env_path, "A" * (ac.HERMES_READ_MAX_BYTES["env"] + 10), 0o600)
        with mock.patch("sys.stderr"):
            self.assertIsNone(ac.read_confined_text("env", self.env_path, **self.kwargs))

    def test_file_outside_every_root_is_refused(self):
        outside = os.path.join(self.home, "outside.env")
        self._write(outside, "X=1\n", 0o600)
        with mock.patch("sys.stderr"):
            self.assertIsNone(ac.read_confined_text("env", outside, **self.kwargs))

    def test_relative_and_traversal_paths_are_refused(self):
        for bad in ("relative.env", os.path.join(self.hermes, "..", "x.env")):
            with mock.patch("sys.stderr"):
                self.assertIsNone(ac.read_confined_text("env", bad, **self.kwargs))

    def test_tilde_traversal_above_the_home_is_refused(self):
        # ~ must expand against the INJECTED home, or this exercises the home
        # resolver instead of the traversal rule it is named for.
        kwargs = dict(self.kwargs)
        with mock.patch("sys.stderr"):
            self.assertIsNone(ac.read_confined_text("env", "~/../escape.env", **kwargs))
        # And a tilde path INSIDE a trusted root is a normal, accepted read —
        # this is the seam that was broken (expansion ignored the injected home).
        target = os.path.join(self.hermes, "ok.env")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("A=1\n")
        os.chmod(target, 0o600)
        with mock.patch("sys.stderr"):
            self.assertEqual(ac.read_confined_text("env", "~/.hermes/ok.env", **kwargs), "A=1\n")

    def test_nul_byte_in_path_is_refused_not_raised(self):
        # A ValueError here would escape a path-error-only handler, empty stdout
        # and reach the widget as a parse error.
        with mock.patch("sys.stderr"):
            self.assertIsNone(ac.read_confined_text("env", self.env_path + "\x00x", **self.kwargs))

    def test_bounded_profile_accepts_a_0644_config(self):
        # The strict recipe on a non-secret file would silently empty the roster.
        cfg = os.path.join(self.hermes, "config.yaml")
        self._write(cfg, "bot_peers:\n  spark:\n    url: http://spark.lan:8377\n", 0o644)
        text = ac.read_confined_text("config", cfg, **self.kwargs)
        self.assertIn("bot_peers", text or "")

    def test_strict_profile_rejects_what_bounded_allows(self):
        # Same file, 0644: the strict profile must still refuse it.
        cfg = os.path.join(self.hermes, "config.yaml")
        self._write(cfg, "bot_peers: {}\n", 0o644)
        with mock.patch("sys.stderr"):
            self.assertIsNone(ac.read_confined_text("env", cfg, **self.kwargs))

    def test_group_writable_root_is_refused(self):
        os.chmod(self.hermes, 0o770)
        with mock.patch("sys.stderr"):
            self.assertIsNone(ac.read_confined_text("env", self.env_path, **self.kwargs))

    def test_registry_read_uses_the_bounded_reader(self):
        ac.read_hermes_registry()
        # A re-pointed reader means the old bare open() is gone; assert the
        # function it replaced is what the module now calls.
        with open(os.path.join(ROOT, "agent_ctl.py"), encoding="utf-8") as handle:
            self.assertNotIn("with open(HERMES_CONNECTIONS_PATH", handle.read())


class PeerKeyLoading(unittest.TestCase):
    def setUp(self):
        self.home, self.hermes = _fixture_home(self)
        self.addCleanup(self._cleanup)
        self.env_path = os.path.join(self.hermes, ".env")
        self.kwargs = dict(roots=[self.hermes], trust_root=self.home,
                           ancestor_uids=SANDBOX_ANCESTOR_UIDS, uid=os.getuid(), home=self.home)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def test_key_is_found_and_never_reaches_os_environ(self):
        with open(self.env_path, "w", encoding="utf-8") as handle:
            handle.write(f"HERMES_PEER_SPARK_KEY={PEER_KEY}\n")
        os.chmod(self.env_path, 0o600)
        os.environ.pop("HERMES_PEER_SPARK_KEY", None)
        with mock.patch.object(ac, "read_confined_text", return_value=f"HERMES_PEER_SPARK_KEY={PEER_KEY}\n"):
            self.assertEqual(ac.load_peer_secret("spark"), PEER_KEY)
        # Assert the EFFECT, not the return value: launch_agent() spawns with no
        # env=, so a key in os.environ would be handed to a child.
        self.assertNotIn("HERMES_PEER_SPARK_KEY", os.environ)

    def test_exported_and_quoted_forms_are_accepted(self):
        body = f'export HERMES_PEER_SPARK_KEY="{PEER_KEY}"\n'
        with mock.patch.object(ac, "read_confined_text", return_value=body):
            self.assertEqual(ac.load_peer_secret("spark"), PEER_KEY)

    def test_splitlines_smuggling_does_not_invent_a_second_assignment(self):
        # \x85 splits a line for str.splitlines() but NOT for split("\n"), so a
        # name smuggled after one would only appear under a splitlines() parser.
        body = f"HERMES_PEER_SPARK={PEER_KEY}\x85HERMES_PEER_SPARK_KEY={PEER_KEY}\n"
        with mock.patch.object(ac, "read_confined_text", return_value=body):
            self.assertIsNone(ac.load_peer_secret("spark"))

    def test_absent_key_is_none_not_empty_string(self):
        with mock.patch.object(ac, "read_confined_text", return_value="OTHER=1\n"):
            self.assertIsNone(ac.load_peer_secret("spark"))

    def test_traversal_shaped_peer_name_is_refused(self):
        self.assertIsNone(ac.load_peer_secret("../../etc/passwd"))
        self.assertIsNone(ac.load_peer_secret(""))


class PeerUrlAndLoopback(unittest.TestCase):
    def test_userinfo_query_fragment_and_path_are_refused(self):
        for bad in ("http://user:pass@host:8377", "http://host:8377?x=1",
                    "http://host:8377#f", "http://host:8377/sub", "file:///etc/passwd",
                    "javascript:alert(1)", "ftp://host", "http://host:notaport", ""):
            self.assertIsNone(ac.peer_base_url(bad), bad)

    def test_trailing_slash_and_case_are_normalised(self):
        self.assertEqual(ac.peer_base_url("HTTP://Spark.LAN:8377/"), "http://spark.lan:8377")

    def test_every_loopback_spelling_is_recognised(self):
        for url in ("http://127.0.0.1:8377", "http://127.1:8377", "http://127.0.53:8377",
                    "http://0x7f000001:8377", "http://2130706433:8377", "http://[::1]:8377",
                    "http://[::ffff:127.0.0.1]:8377", "http://localhost:8377",
                    "http://LOCALHOST.:8377", "http://box.localhost:8377"):
            self.assertTrue(ac.peer_host_is_loopback(url), url)

    def test_remote_hosts_are_not_loopback(self):
        for url in ("http://spark.lan:8377", "http://10.0.0.5:8377", "http://192.168.1.9:8377",
                    "http://8.8.8.8:8377", "http://[2001:db8::1]:8377", ""):
            self.assertFalse(ac.peer_host_is_loopback(url), url)

    def test_userinfo_cannot_steer_the_loopback_check(self):
        self.assertFalse(ac.peer_host_is_loopback("http://u:p@127.0.0.1:8377"))


class PeerParser(unittest.TestCase):
    def test_valid_block_with_note_and_quotes(self):
        text = (
            "model:\n  provider: x\n"
            "bot_peers:\n"
            "  spark:\n    url: http://spark.lan:8377\n    note: homelab, remote\n"
            '  mini-2:\n    url: "https://10.0.0.9:8377"\n'
            "gateway:\n  running: true\n"
        )
        self.assertEqual(ac.parse_bot_peers(text), [
            {"name": "spark", "url": "http://spark.lan:8377"},
            {"name": "mini-2", "url": "https://10.0.0.9:8377"},
        ])

    def test_absent_or_empty_input_degrades_to_nothing(self):
        self.assertEqual(ac.parse_bot_peers(None), [])
        self.assertEqual(ac.parse_bot_peers(""), [])
        self.assertEqual(ac.parse_bot_peers("model:\n  provider: x\n"), [])

    def test_nested_bot_peers_is_not_ours(self):
        self.assertEqual(ac.parse_bot_peers("other:\n  bot_peers:\n    url: http://x:1\n"), [])

    def test_traversal_shaped_name_is_dropped(self):
        text = "bot_peers:\n  ../../etc:\n    url: http://h:1\n  good:\n    url: http://h:1\n"
        self.assertEqual([p["name"] for p in ac.parse_bot_peers(text)], ["good"])

    def test_peer_cap_is_enforced(self):
        lines = ["bot_peers:"]
        for index in range(12):
            lines += [f"  p{index}:", f"    url: http://h{index}:8377"]
        self.assertLessEqual(len(ac.parse_bot_peers("\n".join(lines))), ac.PEER_MAX_PEERS_PARSED)

    def test_row_without_a_url_is_dropped(self):
        self.assertEqual(ac.parse_bot_peers("bot_peers:\n  spark:\n    note: no url\n"), [])

    def test_comments_and_blank_lines_are_skipped(self):
        text = "bot_peers:\n  # a comment\n\n  spark:\n    url: http://h:1  # trailing\n"
        self.assertEqual(ac.parse_bot_peers(text), [{"name": "spark", "url": "http://h:1  # trailing"}])


class PeerSessionIdentity(unittest.TestCase):
    def test_dot_and_dotdot_ids_are_refused(self):
        # quote(safe="") leaves "." alone, so these would build a traversal path.
        for raw in (".", ".."):
            self.assertIsNone(ac.peer_session_id({"id": raw}))

    def test_pct_and_slash_shaped_ids_are_refused(self):
        for raw in ("a/b", "%2e%2e", "x y", "a" * 129, ""):
            self.assertIsNone(ac.peer_session_id({"id": raw}))

    def test_ordinary_id_is_kept(self):
        self.assertEqual(ac.peer_session_id({"id": "20261005_abc-1.2"}), "20261005_abc-1.2")

    def test_request_target_refuses_a_traversal_path(self):
        self.assertIsNone(ac.peer_request_target("http://h:1", "/api/sessions/../messages"))
        self.assertIsNone(ac.peer_request_target("http://h:1", "/api/./sessions"))
        self.assertEqual(
            ac.peer_request_target("http://h:1", "/api/sessions/x/messages"),
            "http://h:1/api/sessions/x/messages")

    def test_target_id_is_peer_qualified(self):
        self.assertEqual(ac.peer_target_id("spark", "s1"), "hermes-peer:spark:s1")


class PeerStatusTable(unittest.TestCase):
    def test_assistant_with_tool_calls_is_working(self):
        status, detail, _ = ac.peer_session_status({"role": "assistant", "tool_calls": "[{}]"}, False)
        self.assertEqual(status, "working")
        self.assertIn("tool", detail.lower())

    def test_tool_message_follows_activity(self):
        active = ac.peer_session_status({"role": "tool", "tool_name": "read_file"}, True)
        idle = ac.peer_session_status({"role": "tool", "tool_name": "read_file"}, False)
        self.assertEqual(active[0], "working")
        self.assertEqual(idle[0], "completed")

    def test_assistant_content_asking_a_question_waits_when_inactive(self):
        status, _, question = ac.peer_session_status(
            {"role": "assistant", "content": "Should I push it?"}, False)
        self.assertEqual(status, "waiting")
        self.assertTrue(question)

    def test_assistant_content_is_working_when_active(self):
        status, _, _ = ac.peer_session_status({"role": "assistant", "content": "On it"}, True)
        self.assertEqual(status, "working")

    def test_user_message_maps_to_idle_when_inactive(self):
        self.assertEqual(ac.peer_session_status({"role": "user", "content": "hi"}, False)[0], "idle")

    def test_unpolled_session_is_unknown_not_working(self):
        # A card we never read a message for must not claim to be working: the
        # local scanner uses the same "unknown" status for the same reason.
        status, detail, question = ac.peer_session_status(None, True)
        self.assertEqual(status, "unknown")
        self.assertIn("Not polled", detail)
        self.assertFalse(question)

    def test_active_heuristic_uses_ended_at_and_the_window(self):
        now = time.time()
        self.assertTrue(ac.peer_session_is_active({"ended_at": None, "last_active": now - 5}, now))
        self.assertFalse(ac.peer_session_is_active({"ended_at": now, "last_active": now - 5}, now))
        self.assertFalse(ac.peer_session_is_active({"ended_at": None, "last_active": now - 400}, now))
        self.assertFalse(ac.peer_session_is_active({"ended_at": None, "last_active": "junk"}, now))

    def test_wrong_type_rows_do_not_raise(self):
        # An unrecognised role falls through to the final branch, which reports
        # activity when the row is active — the point here is that no attribute
        # error escapes on a row whose fields are the wrong type.
        self.assertEqual(ac.peer_session_status({"role": 7, "content": []}, True)[0], "working")
        self.assertEqual(ac.peer_session_status({"role": 7, "content": []}, False)[0], "completed")
        self.assertEqual(ac.parse_peer_sessions({"data": "nope"}), [])
        self.assertEqual(ac.parse_peer_sessions(None), [])
        self.assertIsNone(ac.parse_peer_message({"data": []}))


class PeerScrubbing(unittest.TestCase):
    def test_peer_key_is_scrubbed_even_though_it_is_not_key_shaped(self):
        echoed = f"session {PEER_KEY} done"
        out = ac.scrub_peer_text(echoed, [PEER_KEY])
        self.assertNotIn(PEER_KEY, out)
        self.assertIn("[REDACTED]", out)

    def test_ansi_and_control_bytes_are_removed(self):
        out = ac.scrub_peer_text("a\x1b[31mred\x07b\x00c", [])
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertNotIn("\x00", out)

    def test_shape_keyed_secrets_are_still_redacted(self):
        secret = "sk-" + "a" * 24
        self.assertNotIn(secret, ac.scrub_peer_text(f"key {secret} here", []))

    def test_a_short_sk_prefixed_token_is_below_the_redaction_threshold(self):
        # Documented boundary, not an accident: redact_secrets needs 8+12 chars.
        self.assertIn("sk-abcdefghijklmnop", ac.scrub_peer_text("key sk-abcdefghijklmnop here", []))

    def test_length_is_capped(self):
        self.assertLessEqual(len(ac.scrub_peer_text("x" * 5000, [])), 400)

    def test_a_tiny_secret_does_not_shred_every_string(self):
        self.assertEqual(ac.scrub_peer_text("hello world", ["a"]), "hello world")


class _StubPeer:
    """An injectable peer_http_get replacement: no sockets, deterministic."""

    def __init__(self, sessions=None, messages=None, bot=None, fail=False):
        self.sessions = sessions or []
        self.messages = messages or {}
        self.bot = bot or []
        self.fail = fail
        self.calls = []

    def __call__(self, base, path, key, *, deadline):
        self.calls.append((base, path, key))
        if self.fail:
            return None
        if "include_hidden=1" in path:
            return {"object": "list", "data": self.bot}
        if "/messages" in path:
            session_id = path.split("/api/sessions/")[1].split("/")[0]
            return {"object": "list", "data": self.messages.get(session_id, [])}
        return {"object": "list", "data": self.sessions}


class PeerCardBuilding(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.rows = [
            {"id": "s1", "title": "Fix parser", "model": "opencode-go/glm-5", "source": "api_server",
             "message_count": 9, "last_active": self.now - 5, "ended_at": None},
            {"id": "s2", "title": "Stale", "source": "api_server", "message_count": 3,
             "last_active": self.now - 90000},
            {"id": "..", "title": "traversal", "message_count": 2, "last_active": self.now - 3},
        ]
        self.peer = {"name": "spark", "url": "http://spark.lan:8377", "base": "http://spark.lan:8377"}

    def test_cards_are_read_only_and_namespaced(self):
        stub = _StubPeer(self.rows, {"s1": [{"role": "assistant", "content": "Working on it"}]})
        cards, status = ac.build_peer_agents([self.peer], self.now, keys={"spark": PEER_KEY}, fetch=stub)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["pane_id"], "hermes-peer:spark:s1")
        self.assertEqual(card["origin"], "hermes_peer")
        self.assertEqual(card["origin_badge"], "PEER · spark")
        self.assertFalse(card["can_focus"])
        self.assertFalse(card["can_control"])
        self.assertEqual(status[0]["reachable"], True)

    def test_stale_and_traversal_rows_never_become_cards(self):
        stub = _StubPeer(self.rows, {})
        cards, _ = ac.build_peer_agents([self.peer], self.now, keys={"spark": PEER_KEY}, fetch=stub)
        ids = [card["pane_id"] for card in cards]
        self.assertNotIn("hermes-peer:spark:s2", ids)
        self.assertFalse(any(".." in pane for pane in ids))

    def test_missing_key_yields_a_stated_reason_and_no_card(self):
        stub = _StubPeer(self.rows, {})
        cards, status = ac.build_peer_agents([self.peer], self.now, keys={}, fetch=stub)
        self.assertEqual(cards, [])
        self.assertIn("HERMES_PEER_SPARK_KEY", status[0]["error"])
        self.assertFalse(status[0]["reachable"])

    def test_loopback_peer_is_skipped_with_a_reason(self):
        peer = {"name": "local", "url": "http://127.0.0.1:8377", "base": "http://127.0.0.1:8377"}
        stub = _StubPeer(self.rows, {})
        cards, status = ac.build_peer_agents([peer], self.now, keys={"local": PEER_KEY}, fetch=stub)
        self.assertEqual(cards, [])
        self.assertIn("loopback", status[0]["error"])
        self.assertEqual(stub.calls, [], "a loopback peer must not be queried at all")

    def test_message_read_budget_is_respected(self):
        rows = [{"id": f"s{i}", "title": f"t{i}", "message_count": 2, "last_active": self.now - i}
                for i in range(6)]
        stub = _StubPeer(rows, {})
        ac.build_peer_agents([self.peer], self.now, keys={"spark": PEER_KEY}, fetch=stub)
        reads = [c for c in stub.calls if "/messages" in c[1]]
        self.assertLessEqual(len(reads), ac.PEER_MAX_MESSAGE_READS)

    def test_cards_per_peer_are_capped(self):
        rows = [{"id": f"s{i}", "title": f"t{i}", "message_count": 2, "last_active": self.now - i}
                for i in range(20)]
        stub = _StubPeer(rows, {})
        cards, _ = ac.build_peer_agents([self.peer], self.now, keys={"spark": PEER_KEY}, fetch=stub)
        self.assertLessEqual(len(cards), ac.PEER_MAX_PEER_CARDS)

    def test_hostile_peer_text_is_scrubbed_in_every_field(self):
        rows = [{"id": "s1", "title": f"echo {PEER_KEY}", "model": f"x/{PEER_KEY}",
                 "source": f"src {PEER_KEY}", "message_count": 2, "last_active": self.now - 5}]
        stub = _StubPeer(rows, {"s1": [{"role": "tool", "tool_name": f"t_{PEER_KEY}"}]})
        cards, _ = ac.build_peer_agents([self.peer], self.now, keys={"spark": PEER_KEY}, fetch=stub)
        blob = json.dumps(cards)
        self.assertNotIn(PEER_KEY, blob)

    def test_bot_chat_is_requested_by_title_so_hidden_rows_are_visible(self):
        stub = _StubPeer([], bot=[{"id": "bot", "title": "Bot Chat", "message_count": 2,
                                   "last_active": self.now - 5}])
        ac.build_peer_agents([self.peer], self.now, keys={"spark": PEER_KEY}, fetch=stub)
        self.assertTrue(any("include_hidden=1" in call[1] for call in stub.calls))

    def test_a_failing_peer_does_not_raise(self):
        stub = _StubPeer(fail=True)
        cards, status = ac.build_peer_agents([self.peer], self.now, keys={"spark": PEER_KEY}, fetch=stub)
        self.assertEqual(cards, [])
        self.assertTrue(status[0]["error"])

    def test_forty_cards_stay_inside_the_payload_ceiling(self):
        rows = [{"id": f"s{i}", "title": f"task {i}", "message_count": 2, "last_active": self.now - i}
                for i in range(40)]
        stub = _StubPeer(rows, {})
        cards, _ = ac.build_peer_agents([self.peer], self.now, keys={"spark": PEER_KEY}, fetch=stub)
        payload = ac.dump_status_json({
            "ok": True, "agents": cards, "summary": {"total": len(cards)},
            "hermes_peers": [{"name": "spark", "url": "http://spark.lan:8377", "reachable": True,
                              "sessions": len(cards), "error": ""}],
        })
        self.assertLessEqual(len(payload), ac.STATUS_MAX_BYTES)
        json.loads(payload)

    def test_oversized_payload_fallback_keeps_the_peer_row_shape(self):
        payload = ac.dump_status_json({
            "ok": True, "agents": [{"title": "x" * 400} for _ in range(256)],
            "summary": {"total": 256, "working": 1},
            "hermes_peers": [{"name": "spark", "url": "http://h:1", "reachable": True,
                              "sessions": 3, "error": "x" * 500}],
        })
        data = json.loads(payload)
        self.assertTrue(data["agents"] == [] or len(payload) <= ac.STATUS_MAX_BYTES)
        self.assertIn("hermes_peers", data)


class PeerTransportAgainstRealServer(unittest.TestCase):
    """The two claims a fake CANNOT prove, against a real loopback server.

    This suite opens a listener on an ephemeral 127.0.0.1 port (documented: it is
    not socket-free) and is cleaned up in tearDown.
    """

    @classmethod
    def setUpClass(cls):
        now = time.time()
        cls.sessions = {"object": "list", "data": [
            {"id": "s1", "title": "t", "message_count": 1, "last_active": now}]}

        class Handler(BaseHTTPRequestHandler):
            seen_paths = []

            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

            def do_GET(self):  # noqa: N802
                type(self).seen_paths.append(self.path)  # wire-level assertion needs this
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "http://elsewhere.invalid/x")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/huge"):
                    blob = b"x" * (600 * 1024)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(blob)))
                    self.end_headers()
                    self.wfile.write(blob)
                    return
                if self.path.startswith("/dribble"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    try:
                        for _ in range(400):          # ~20 s, far past the budget
                            self.wfile.write(b" ")
                            self.wfile.flush()
                            time.sleep(0.05)
                    except Exception:
                        pass
                    return
                body = json.dumps(cls.sessions).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.handler_cls = Handler
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def server_paths(self):
        return self.__class__.handler_cls.seen_paths

    def setUp(self):
        self.server_paths.clear()

    def test_the_query_string_reaches_the_wire(self):
        # A precedence bug once sent every request as a bare "/api/sessions",
        # silently dropping limit / order=latest / include_hidden / title — and
        # every stub-based test still passed, because a stub matches the string it
        # was handed and never sees the wire. Assert what the SERVER received.
        for path in ("/api/sessions?limit=20",
                     "/api/sessions?title=Bot%20Chat&include_hidden=1&limit=4",
                     "/api/sessions/s1/messages?limit=1&order=latest"):
            self.server_paths.clear()
            ac.peer_http_get(self.base, path, PEER_KEY, deadline=time.monotonic() + 3)
            self.assertEqual(self.server_paths, [path])

    def test_a_stalling_resolver_is_cut_off_at_the_budget(self):
        # connect() resolves the name before any socket exists, so the response
        # watchdog cannot cover it: a 20 s resolve was measured against a 3 s
        # deadline. The pre-resolve is what bounds it.
        real = socket.getaddrinfo

        def stall(*args, **kwargs):
            time.sleep(20)
            raise OSError("stalled")

        with mock.patch.object(ac.socket, "getaddrinfo", stall):
            started = time.monotonic()
            result = ac.peer_http_get("http://blackhole.invalid:8377", "/api/sessions",
                                      PEER_KEY, deadline=time.monotonic() + 1.0)
            elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, 3.0, f"DNS stall not bounded (took {elapsed:.2f}s)")

    def test_a_normal_response_parses(self):
        result = ac.peer_http_get(self.base, "/api/sessions?limit=2", PEER_KEY,
                                   deadline=time.monotonic() + 3)
        self.assertEqual(result["data"][0]["id"], "s1")

    def test_redirect_is_refused_and_the_key_is_not_resent(self):
        # A 3xx must be a refusal: urllib would re-send Authorization to the
        # redirect target, handing the peer key to an unregistered host.
        self.assertIsNone(ac.peer_http_get(self.base, "/redirect", PEER_KEY,
                                           deadline=time.monotonic() + 3))

    def test_oversized_body_is_refused_before_decoding(self):
        self.assertIsNone(ac.peer_http_get(self.base, "/huge", PEER_KEY,
                                           deadline=time.monotonic() + 3))

    def test_dribbling_peer_is_cut_off_at_the_total_budget(self):
        for budget in (1.0, 2.0):
            started = time.monotonic()
            result = ac.peer_http_get(self.base, "/dribble", PEER_KEY,
                                      deadline=time.monotonic() + budget)
            elapsed = time.monotonic() - started
            self.assertIsNone(result)
            self.assertLess(elapsed, budget + 1.5,
                            f"budget {budget}s not enforced (took {elapsed:.2f}s)")

    def test_unreachable_peer_is_none_not_an_exception(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()
        self.assertIsNone(ac.peer_http_get(f"http://127.0.0.1:{dead_port}", "/api/sessions",
                                           PEER_KEY, deadline=time.monotonic() + 2))


class PeerWireRecording(unittest.TestCase):
    """Asserts the EXACT request line a real socket carried.

    Distinct from the stub-based tests on purpose: those intercept the string
    handed to an injected fetch and therefore cannot see a bug in how that
    string becomes an HTTP request. These stand up a server and read
    self.requestline back.
    """

    def setUp(self):
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def do_GET(self):  # noqa: N802
                outer.seen.append(self.path)
                body = json.dumps({"object": "list", "data": []}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def test_sessions_query_carries_limit(self):
        ac.peer_http_get(self.base(), f"/api/sessions?limit={ac.PEER_SESSIONS_QUERY}", PEER_KEY,
                         deadline=time.monotonic() + 3)
        self.assertEqual(self.seen, [f"/api/sessions?limit={ac.PEER_SESSIONS_QUERY}"])

    def test_message_read_carries_order_latest(self):
        ac.peer_http_get(self.base(), "/api/sessions/s1/messages?limit=1&order=latest", PEER_KEY,
                         deadline=time.monotonic() + 3)
        self.assertEqual(self.seen, ["/api/sessions/s1/messages?limit=1&order=latest"])

    def test_bot_chat_query_carries_include_hidden(self):
        ac.peer_http_get(self.base(), f"/api/sessions?title=Bot%20Chat&include_hidden=1&limit=4",
                         PEER_KEY, deadline=time.monotonic() + 3)
        self.assertIn("include_hidden=1", self.seen[0])
        self.assertIn("title=Bot", self.seen[0])

    def test_build_peer_agents_sends_the_hidden_filter_for_real(self):
        # Drive the REAL transport (not a stub) through the card builder. The test
        # server is on 127.0.0.1, which the dedup rule skips, so that one guard is
        # bypassed here on purpose — the rule itself is asserted in
        # PeerCardBuilding.test_loopback_peer_is_skipped_with_a_reason.
        with mock.patch.object(ac, "peer_host_is_loopback", return_value=False):
            cards, status = ac.build_peer_agents(
                [{"name": "spark", "url": self.base(), "base": self.base()}],
                time.time(), keys={"spark": PEER_KEY},
            )
        self.assertTrue(any("include_hidden=1" in path for path in self.seen), self.seen)
        self.assertEqual(cards, [])   # the server reports no sessions
        self.assertTrue(status[0]["reachable"], status)


class PeerUnreachableIsNotEmpty(unittest.TestCase):
    """A peer that cannot be reached must not be reported as a healthy empty one."""

    def test_none_first_response_means_unreachable(self):
        class Dead:
            def __call__(self, base, path, key, *, deadline):
                return None
        cards, status = ac.build_peer_agents(
            [{"name": "spark", "url": "http://spark.lan:8377", "base": "http://spark.lan:8377"}],
            time.time(), keys={"spark": PEER_KEY}, fetch=Dead(),
        )
        self.assertEqual(cards, [])
        self.assertFalse(status[0]["reachable"], status)
        self.assertIn("unreachable", status[0]["error"])

    def test_empty_but_answered_is_reachable(self):
        class Empty:
            def __call__(self, base, path, key, *, deadline):
                return {"object": "list", "data": []}
        cards, status = ac.build_peer_agents(
            [{"name": "spark", "url": "http://spark.lan:8377", "base": "http://spark.lan:8377"}],
            time.time(), keys={"spark": PEER_KEY}, fetch=Empty(),
        )
        self.assertEqual(cards, [])
        self.assertTrue(status[0]["reachable"], status)


class PeerParserAttribution(unittest.TestCase):
    """A rejected or over-cap entry must not lend its url to the previous peer."""

    def test_rejected_name_does_not_donate_its_url(self):
        text = ("bot_peers:\n  spark:\n    url: http://good.lan:8377\n"
                "  ../../etc:\n    url: http://evil.lan:8377\n"
                "  mini:\n    url: http://mini.lan:8377\n")
        rows = ac.parse_bot_peers(text)
        self.assertEqual([r["name"] for r in rows], ["spark", "mini"])
        self.assertEqual(rows[0]["url"], "http://good.lan:8377")

    def test_entry_past_the_cap_does_not_repoint_a_kept_peer(self):
        lines = ["bot_peers:"]
        for index in range(12):
            lines += [f"  p{index}:", f"    url: http://h{index}.lan:8377"]
        rows = ac.parse_bot_peers("\n".join(lines))
        self.assertEqual(len(rows), ac.PEER_MAX_PEERS_PARSED)
        self.assertEqual(rows[-1]["url"], f"http://h{ac.PEER_MAX_PEERS_PARSED - 1}.lan:8377")


class PeerTargetAuthority(unittest.TestCase):
    def test_scheme_relative_path_cannot_replace_the_host(self):
        # urljoin treats "//host/x" as absolute, which would send the Bearer key
        # to a host the user never registered.
        self.assertIsNone(ac.peer_request_target("http://spark.lan:8377", "//evil.example/steal"))

    def test_normal_path_still_works(self):
        self.assertEqual(ac.peer_request_target("http://h:1", "/api/sessions?limit=2"),
                         "http://h:1/api/sessions?limit=2")


class PeerNonFiniteActivity(unittest.TestCase):
    def test_nan_and_inf_last_active_never_become_a_card(self):
        now = time.time()
        for bogus in (float("nan"), float("inf"), -float("inf"), "nan", "1e999"):
            cards = ac._cards_from_rows(
                "spark", "http://spark.lan:8377", PEER_KEY,
                [{"id": "s1", "title": "t", "message_count": 2, "last_active": bogus}],
                now, lambda *a, **k: None, time.monotonic() + 3,
            )
            self.assertEqual(cards, [], f"{bogus!r} produced a card")

    def test_ordinary_recent_row_still_becomes_a_card(self):
        now = time.time()
        cards = ac._cards_from_rows(
            "spark", "http://spark.lan:8377", PEER_KEY,
            [{"id": "s1", "title": "t", "message_count": 2, "last_active": now - 5}],
            now, lambda *a, **k: None, time.monotonic() + 3,
        )
        self.assertEqual(len(cards), 1)


class PeerKeyFloor(unittest.TestCase):
    def test_a_short_key_is_refused_rather_than_leaking_through_the_scrubber(self):
        # scrub_peer_text only redacts literals of >=8 chars, so a shorter key
        # would bypass it and could be echoed into the bar by the peer.
        with mock.patch.object(ac, "read_confined_text",
                               return_value="HERMES_PEER_SPARK_KEY=abc1234\n"):
            self.assertIsNone(ac.load_peer_secret("spark"))
        with mock.patch.object(ac, "read_confined_text",
                               return_value=f"HERMES_PEER_SPARK_KEY={PEER_KEY}\n"):
            self.assertEqual(ac.load_peer_secret("spark"), PEER_KEY)


class PeerTlsTrustStore(unittest.TestCase):
    def test_a_missing_bundle_fails_closed_instead_of_using_the_env(self):
        # The removed fallback called create_default_context() with no cafile,
        # which honours SSL_CERT_FILE — so a host without the bundle would have
        # had the peer's trust store decided by the session environment.
        with mock.patch.object(ac, "PEER_CA_BUNDLE", "/nonexistent/ca-bundle.crt"), \
             mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/etc/hostname"}):
            self.assertIsNone(ac._peer_tls_context())

    def test_the_pinned_bundle_is_used_when_present(self):
        if not os.path.exists(ac.PEER_CA_BUNDLE):
            self.skipTest(f"{ac.PEER_CA_BUNDLE} absent on this host")
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/etc/hostname"}):
            context = ac._peer_tls_context()
        self.assertIsNotNone(context)
        # The env var is ignored, so the store is the system bundle, not /etc/hostname.
        self.assertGreater(len(context.get_ca_certs()), 0)


class PeerReadOnlyContract(unittest.TestCase):
    def test_focus_and_kill_refuse_a_peer_target(self):
        target = ac.peer_target_id("spark", "s1")
        with mock.patch.object(ac, "get_hypr_clients", return_value=[]), \
             mock.patch.object(ac, "get_hypr_env", return_value={}), \
             mock.patch.object(ac.os, "kill") as killer:
            self.assertFalse(ac.focus_pane(target)["ok"])
            self.assertFalse(ac.kill_target(target)["ok"])
            killer.assert_not_called()

    def test_a_peer_prefixed_id_never_reaches_a_pid_branch(self):
        # "hermes-peer:spark:terminal:pid:1234" must not fall through to the
        # pid-signalling path just because it contains a pid-bearing substring.
        target = "hermes-peer:spark:terminal:pid:1"
        with mock.patch.object(ac, "get_hypr_clients", return_value=[]), \
             mock.patch.object(ac, "get_hypr_env", return_value={}), \
             mock.patch.object(ac.os, "kill") as killer:
            self.assertFalse(ac.kill_target(target)["ok"])
            killer.assert_not_called()

    def test_existing_herdr_remote_refusal_still_holds(self):
        self.assertFalse(ac.focus_pane("herdr-remote:m1:default|w1:p1")["ok"])
        self.assertFalse(ac.kill_target("herdr-remote:m1:default|w1:p1")["ok"])


class PeerGatewayAnnotation(unittest.TestCase):
    def test_matching_url_gains_the_peer_and_a_session_count(self):
        gateways = [{"id": "r1", "kind": "remote", "label": "Infra", "url": "http://spark.lan:8377"}]
        status = [{"name": "spark", "url": "http://spark.lan:8377", "reachable": True,
                   "sessions": 3, "error": ""}]
        rows = ac.annotate_gateways_with_peers(gateways, status)
        self.assertEqual(rows[0]["peer"], "spark")
        self.assertEqual(rows[0]["session_count"], "3")

    def test_a_different_url_is_left_alone(self):
        gateways = [{"id": "r1", "kind": "remote", "label": "Other", "url": "http://other.lan:8377"}]
        status = [{"name": "spark", "url": "http://spark.lan:8377", "reachable": True,
                   "sessions": 3, "error": ""}]
        self.assertNotIn("peer", ac.annotate_gateways_with_peers(gateways, status)[0])

    def test_no_peers_leaves_the_registry_untouched(self):
        gateways = [{"id": "r1", "kind": "remote", "label": "Infra", "url": "http://spark.lan:8377"}]
        self.assertEqual(ac.annotate_gateways_with_peers(gateways, []), gateways)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
