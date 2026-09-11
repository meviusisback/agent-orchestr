#!/usr/bin/env python3
"""
agent_ctl.py - Backend collector and orchestrator controller for Omarchy Agent Orchestrator.
Discovers and manages AI agents across:
1. Herdr workspaces & panes (Herdr daemon socket + process tree correlation)
2. Standard terminal windows (Foot, Alacritty, Kitty, Ghostty)
3. Hermes Desktop GUI instances (Electron app)
4. Grok Build TUI sessions ($GROK_HOME/active_sessions.json + events.jsonl)
"""

import glob
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, unquote

HERDR_SOCK_PATH = os.path.expanduser(os.environ.get("HERDR_SOCKET_PATH", "~/.config/herdr/herdr.sock"))
OMP_SESSIONS_DIR = os.path.expanduser("~/.omp/agent/sessions")
HERMES_STATE_DB = os.path.expanduser("~/.hermes/state.db")

# Claude Code keeps no session .jsonl file descriptor open the way OMP does and
# exposes no local session API the way Hermes does, so find_session_for_process()
# never resolves a session for it and every claude process falls back to the
# generic idle default regardless of its real state. Claude Code hooks (see
# hooks/claude-code-status.sh) write real status here instead, one file per
# session, keyed by the claude process's own PID.
CLAUDE_HOOK_STATUS_DIR = os.path.expanduser("~/.local/state/omarchy/agents/claude-status")
CLAUDE_HOOK_STATUS_MAX_AGE = 24 * 3600
CLAUDE_HOOK_STATUSES = ("working", "waiting", "completed", "idle")

KNOWN_TERMINALS = (
    "foot", "ghostty", "alacritty", "kitty", "wezterm", "gnome-terminal",
    "ptyxis", "konsole", "terminator", "xfce4-terminal", "xterm", "rio",
    "contour", "blackbox", "tmux"
)

def redact_secrets(text: str) -> str:
    """Redact common API keys, tokens, and credentials from display text."""
    if not text:
        return ""
    t = text
    # OpenAI / Anthropic / Groq / OpenRouter keys
    t = re.sub(r"\b(sk-[a-zA-Z0-9_-]{8})[a-zA-Z0-9_-]{12,}\b", r"\1…[REDACTED]", t)
    # GitHub tokens
    t = re.sub(r"\b(ghp_[a-zA-Z0-9]{4})[a-zA-Z0-9]{16,}\b", r"\1…[REDACTED]", t)
    t = re.sub(r"\b(github_pat_[a-zA-Z0-9_]{4})[a-zA-Z0-9_]{16,}\b", r"\1…[REDACTED]", t)
    # AWS Access Key IDs
    t = re.sub(r"\b(AKIA[0-9A-Z]{4})[0-9A-Z]{12}\b", r"\1…[REDACTED]", t)
    # Bearer tokens
    t = re.sub(r"(Bearer\s+)[a-zA-Z0-9._~+/-]{16,}", r"\1[REDACTED]", t, flags=re.IGNORECASE)
    # Inline key-value tokens (e.g., api_key = "...", token: '...')
    t = re.sub(r"((?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"])[^'\"]{8,}(['\"])", r"\1[REDACTED]\2", t, flags=re.IGNORECASE)
    return t



def clean_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text."""
    if not text:
        return ""
    ansi_regex = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_regex.sub("", text)


def clean_title(title: str) -> str:
    """Clean Braille spinners, prompt prefixes, and terminal noise."""
    if not title:
        return ""
    t = clean_ansi(title).strip()
    t = re.sub(r"^[\u2800-\u28FF\s]+", "", t)
    t = re.sub(r"^[π\s>#$:]+", "", t).strip()
    t = re.sub(r"^[\u2800-\u28FF\s]+", "", t).strip()
    if t.startswith("alberto@omarchy:"):
        t = t.replace("alberto@omarchy:", "").strip()
    t = t.lstrip("> -:").strip()
    return t


def clean_model_name(model_str: Optional[str]) -> str:
    """Strip common provider prefixes for a clean model badge."""
    if not model_str:
        return ""
    m = str(model_str).strip()
    if "/" in m:
        parts = m.split("/")
        # Provider prefixes that only add noise to a small badge. `xai` is Grok's
        # own provider id, and Grok session summaries may carry it.
        if parts[0] in ("google-antigravity", "openrouter", "anthropic", "openai", "deepseek", "groq", "together", "xai"):
            m = "/".join(parts[1:])
    return m

def get_omp_default_model() -> str:
    """Extract default configured model for OMP from config.yml or fallback."""
    cfg_path = os.path.expanduser("~/.omp/agent/config.yml")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r"default:\s*([^\s\n\r]+)", content)
            if m:
                return clean_model_name(m.group(1).strip("\"'"))
            m = re.search(r"defaultModel:\s*([^\s\n\r]+)", content)
            if m:
                return clean_model_name(m.group(1).strip("\"'"))
        except Exception:
            pass
    return "gemini-3.7-flash"


def herdr_session_sockets() -> List[Tuple[str, str]]:
    """Return (session_name, socket_path) for every running Herdr server.

    The default session listens on ~/.config/herdr/herdr.sock; each named
    session gets its own server and socket under
    ~/.config/herdr/sessions/<name>/herdr.sock. Sockets only exist while the
    server for that session is running.
    """
    sessions: List[Tuple[str, str]] = []
    if os.path.exists(HERDR_SOCK_PATH):
        sessions.append(("default", HERDR_SOCK_PATH))
    for sock in sorted(glob.glob(os.path.expanduser("~/.config/herdr/sessions/*/herdr.sock"))):
        sessions.append((os.path.basename(os.path.dirname(sock)), sock))
    return sessions


def herdr_socket_for_session(session_name: str) -> str:
    """Resolve the Herdr socket path for a session name (fallback: default)."""
    for name, sock in herdr_session_sockets():
        if name == session_name:
            return sock
    return HERDR_SOCK_PATH


def query_herdr_socket(method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 1.0, sock_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Send JSON-RPC request to a Herdr socket and return parsed response."""
    sock = sock_path or HERDR_SOCK_PATH
    if not os.path.exists(sock):
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(min(timeout, 0.5))
        s.connect(sock)
        req_id = f"orchestr:{int(time.time() * 1000)}"
        payload = {"id": req_id, "method": method, "params": params or {}}
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))

        max_bytes = 262144  # 256KB max response limit
        data = b""
        while b"\n" not in data and len(data) < max_bytes:
            chunk = s.recv(min(32768, max_bytes - len(data)))
            if not chunk:
                break
            data += chunk
        s.close()

        if not data:
            return None
        return json.loads(data.decode("utf-8", errors="replace"))
    except Exception:
        return None


def get_hypr_env() -> Dict[str, str]:
    """Ensure HYPRLAND_INSTANCE_SIGNATURE and XDG_RUNTIME_DIR are set for hyprctl."""
    env = dict(os.environ)
    runtime_dir = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env["XDG_RUNTIME_DIR"] = runtime_dir
    if "HYPRLAND_INSTANCE_SIGNATURE" not in env or not env["HYPRLAND_INSTANCE_SIGNATURE"]:
        for s in glob.glob(f"{runtime_dir}/hypr/*"):
            if os.path.isdir(s) and os.path.exists(f"{s}/.socket.sock"):
                env["HYPRLAND_INSTANCE_SIGNATURE"] = os.path.basename(s)
                break
    return env


def get_hypr_clients() -> List[Dict[str, Any]]:
    """Fetch all open client windows from Hyprland."""
    env = get_hypr_env()
    try:
        out = subprocess.check_output(["hyprctl", "-j", "clients"], env=env, timeout=0.5).decode()
        return json.loads(out)
    except Exception:
        return []


def focus_hypr_window(client: Dict[str, Any]) -> bool:
    """Switch Hyprland to the client window's workspace and focus its address using Omarchy Lua dispatchers."""
    if not client:
        return False
    env = get_hypr_env()
    ws = client.get("workspace", {})
    ws_id = ws.get("id")
    addr = client.get("address")

    # 1. Switch Hyprland workspace using Omarchy Lua dispatcher
    if ws_id is not None and re.fullmatch(r"-?[0-9]+", str(ws_id)):
        try:
            lua_ws = f'hl.dsp.focus({{ workspace = "{ws_id}" }})'
            subprocess.run(
                ["hyprctl", "dispatch", lua_ws],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=0.5,
            )
        except Exception:
            pass

    # 2. Focus the exact window address using Omarchy Lua dispatcher
    if addr and re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", str(addr)):
        try:
            lua_win = f'hl.dsp.focus({{ window = "address:{addr}" }})'
            subprocess.run(
                ["hyprctl", "dispatch", lua_win],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=0.5,
            )
        except Exception:
            pass
    return True


def is_hermes_cli_process(cmd: str) -> bool:
    """Detect Hermes CLI processes across all invocation forms.

    Covers:
      - python -m hermes_cli.main (direct module invocation)
      - hermes desktop wrapper (Electron app launcher)
      - /path/hermes-agent/hermes script (standalone CLI via bash wrapper)
    """
    return (
        "hermes_cli.main" in cmd
        or "hermes desktop" in cmd
        or "/hermes" in cmd
    )


# Claude Code runs internal helpers under the same `claude` binary (argv[0]).
# They are identifiable by a literal subcommand verb in argv[1]; real sessions
# are invoked as `claude --session-id …`, `claude --resume …` or bare `claude`,
# where that slot holds a flag, a prompt, or nothing. (Ref: issue #10)
CLAUDE_HELPER_SUBCOMMANDS = ("daemon", "bg-pty-host", "bg-spare")


def is_claude_helper_process(cmd: str, argv: Optional[List[str]] = None) -> bool:
    """Heuristic: True for Claude Code internal helpers (daemon, bg-pty-host, bg-spare).

    Helpers share argv[0] == "claude" with real sessions but carry a literal
    subcommand verb in argv[1]; real sessions have a flag, a prompt, or nothing
    there. Pass the exact /proc argv list (get_process_info()["argv"]) so a
    prompt whose first word is e.g. "daemon" — a single argv element — is not
    mistaken for the verb; without argv, cmd.split() is used as a best-effort
    approximation (exact token match, no substring/prefix matching).
    """
    tokens = list(argv) if argv is not None else cmd.split()
    if len(tokens) < 2 or os.path.basename(tokens[0]) != "claude":
        return False
    return tokens[1] in CLAUDE_HELPER_SUBCOMMANDS


# Grok Build's interactive TUI is argv[0] == "grok" with a flag, a prompt, or
# nothing in argv[1]. The same binary also hosts one-shot CLI verbs (login,
# mcp, doctor, agent, …) and headless single-turn runs (`grok -p …`).
# Discriminate on exact argv tokens the same way Claude helpers are
# (issue #10 / PR #11): a prompt whose first word is "agent" is a single argv
# element and must not be mistaken for `grok agent`.
GROK_HELPER_SUBCOMMANDS = (
    "agent", "clone", "completions", "dashboard", "doctor", "du", "disk-usage",
    "export", "help", "inspect", "leader", "login", "logout", "mcp", "memory",
    "models", "plugin", "sessions", "setup", "trace", "update", "usage",
    "version", "v", "worktree", "wrap",
)
# Tokens that mark a non-interactive invocation: headless single-turn runs and
# info-only flags. They are matched anywhere in argv (a `grok --model x -p "…"`
# run is still not a TUI session), which costs us a prompt that is *exactly*
# one of these tokens — the same tradeoff the Claude helper filter accepts, and
# it fails in the safe direction (no card beats a wrong card).
GROK_NON_INTERACTIVE_FLAGS = (
    "-p", "--single", "--prompt-file", "--prompt-json", "--output-format",
    "--json-schema", "-v", "--version", "-h", "--help", "--show-current",
)
GROK_ARG_SCAN_LIMIT = 64            # argv elements inspected when classifying
GROK_SESSION_ID_RE = re.compile(r"\A[0-9a-fA-F-]{8,64}\Z")
GROK_ROSTER_MAX_BYTES = 256 * 1024
GROK_SUMMARY_MAX_BYTES = 1024 * 1024
GROK_CWD_MARKER_MAX_BYTES = 4096
GROK_JSONL_TAIL_BYTES = 65536
GROK_JSONL_MAX_LINE_BYTES = 512 * 1024
GROK_MAX_GROUP_DIRS = 512
GROK_MAX_GROUP_ENTRIES = 512
# Marker files are only needed for the slug+hash (long path) layout. They get
# their own allowance, sized so every group the scan is willing to visit can
# still be read — a marker flood must not be able to hide a real long-path
# session. Marker bytes are a separate bucket, so they never starve the session
# summary/events reads that share the main cycle budget.
GROK_MARKER_MAX_TOTAL_BYTES = GROK_MAX_GROUP_DIRS * GROK_CWD_MARKER_MAX_BYTES
# How many marker-matched groups may be enumerated for one cwd. Markers are
# attacker-controllable text, so a pile of groups all claiming the same cwd must
# not be able to spend the cycle budget (or take over the card) by volume.
GROK_MAX_MATCHED_GROUPS = 8
GROK_CYCLE_MAX_BYTES = 2 * 1024 * 1024
# A marker-bearing tree of GROK_MAX_GROUP_DIRS groups costs ~3 ops per group for
# the trust walks plus one listing, so 512 groups need ~1.6k ops; the ceiling is
# set well above that so several cards can resolve in one tick without any of
# them silently losing their session.
GROK_CYCLE_MAX_OPS = 16384
GROK_CACHE_MAX_ENTRIES = 128
GROK_WAITING_TOOLS = ("ask_user_question",)
GROK_WORKING_PHASES = (
    "waiting_for_model", "streaming_reasoning", "streaming_text", "tool_execution",
)
GROK_WAITING_PHASES = ("permission_prompt",)


def grok_home() -> str:
    """Grok's data dir; GROK_HOME overrides, default ~/.grok."""
    return os.path.expanduser(os.environ.get("GROK_HOME", "~/.grok"))


def is_grok_helper_process(cmd: str, argv: Optional[List[str]] = None) -> bool:
    """True for `grok` invocations that are not an interactive TUI session.

    Two classes: CLI verbs, identified by an exact argv[1] match, and
    headless/info-only runs, identified by an exact flag token anywhere in the
    scanned argv. Exact token matching only — never substring or prefix
    matching, so `grok "fix the agent loop"` stays a session.
    """
    tokens = list(argv) if argv is not None else cmd.split()
    if len(tokens) < 2 or os.path.basename(tokens[0]) != "grok":
        return False
    rest = tokens[1:1 + GROK_ARG_SCAN_LIMIT]
    if rest[0] in GROK_HELPER_SUBCOMMANDS:
        return True
    return any(tok in GROK_NON_INTERACTIVE_FLAGS for tok in rest)


def grok_argv_has_cwd_override(argv: Optional[List[str]]) -> bool:
    """True when the invocation passes `--cwd`, so /proc cwd != session cwd."""
    for tok in (argv or [])[:GROK_ARG_SCAN_LIMIT]:
        if tok == "--cwd" or tok.startswith("--cwd="):
            return True
    return False


class _GrokBudget:
    """Per-fetch-cycle read budget and memo for Grok session files.

    The bar collector re-runs agent_ctl.py every few seconds against whatever
    exists under $GROK_HOME, so a single tick must never become unbounded work:
    these counters cap the total bytes read and the number of file/dir
    operations, and parsed JSON is memoized by (path, mtime, size) so a large
    session tree costs one read per change rather than one per card. Counters
    reset at the start of every fetch_all_agents() call.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.bytes_read = 0
        self.ops = 0
        # Parsed-JSON memo keyed by (path, inode, mtime_ns, size, cap), plus
        # path-level memos so resolving several cards against one session tree
        # inside a tick does not re-walk and re-stat every group directory: the
        # trust cache holds the verdict per absolute path, the group list and the
        # group -> `.cwd` mapping are built once. All stay bounded because every
        # cache miss pays the op budget.
        self.cache: Dict[Tuple[str, int, int, int, int, int], Any] = {}
        self.trusted: Dict[str, bool] = {}
        self.group_dirs: Optional[List[str]] = None
        self.group_cwd: Dict[str, Optional[str]] = {}
        self.marker_bytes = 0

    def exhausted(self) -> bool:
        return self.bytes_read > GROK_CYCLE_MAX_BYTES or self.ops > GROK_CYCLE_MAX_OPS

    def spend(self, nbytes: int, ops: int = 1) -> bool:
        self.bytes_read += max(0, int(nbytes))
        self.ops += max(0, int(ops))
        return not self.exhausted()

    def get(self, key: Tuple[str, int, int, int, int, int]) -> Any:
        # Touch on hit so eviction is LRU rather than FIFO: with a FIFO, a working
        # set larger than the cache re-reads files on every look-up.
        if key not in self.cache:
            return None
        value = self.cache.pop(key)
        self.cache[key] = value
        return value

    def has(self, key: Tuple[str, int, int, int, int, int]) -> bool:
        return key in self.cache

    def store(self, key: Tuple[str, int, int, int, int, int], value: Any) -> Any:
        # Evict the oldest entry rather than refusing to store: a machine with
        # more sessions than the cache holds would otherwise re-read and re-parse
        # the same files on every lookup, which is exactly what exhausts the byte
        # budget on the machines this memo exists to protect.
        while len(self.cache) >= GROK_CACHE_MAX_ENTRIES:
            try:
                self.cache.pop(next(iter(self.cache)))
            except (StopIteration, KeyError):
                break
        self.cache[key] = value
        return value


_GROK_BUDGET = _GrokBudget()


def grok_trusted_path(path: str) -> bool:
    """True when `path` sits inside GROK_HOME with no symlinked or foreign component.

    The plugin only ever *reads* Grok session data, so a path that is a symlink,
    owned by another user or group/world-writable is never trusted: enrichment
    degrades to "no card / no detail" instead of following a redirect. (Same
    standard the Claude status-dir hardening applies to its write path.)
    Verdicts are memoized for the duration of one fetch cycle — the op budget
    bounds how many distinct paths can be walked, so the memo cannot grow past
    that.
    """
    if _GROK_BUDGET.exhausted():
        return False
    try:
        target = os.path.abspath(path)
    except (OSError, ValueError):
        return False
    cached = _GROK_BUDGET.trusted.get(target)
    if cached is not None:
        return cached
    verdict = _grok_trusted_path_uncached(target)
    _GROK_BUDGET.trusted[target] = verdict
    return verdict


def _grok_trusted_path_uncached(target: str) -> bool:
    """Walk every component of an absolute path up to (and including) GROK_HOME."""
    home = os.path.abspath(grok_home())
    if target != home and not target.startswith(home + os.sep):
        return False
    uid = os.getuid()
    current = target
    try:
        while True:
            st = os.lstat(current)
            if stat.S_ISLNK(st.st_mode) or st.st_uid != uid or (st.st_mode & 0o022):
                return False
            if not _GROK_BUDGET.spend(0, 1):
                return False
            if current == home:
                return True
            parent = os.path.dirname(current)
            if parent == current:
                return False
            current = parent
    except (OSError, ValueError):
        return False


def _grok_fd_within_home(fd: int) -> bool:
    """Containment check on the opened fd's real target, not on a pre-open path."""
    try:
        resolved = os.path.realpath(f"/proc/self/fd/{fd}")
    except (OSError, ValueError):
        return False
    home = os.path.realpath(grok_home())
    return resolved == home or resolved.startswith(home + os.sep)


# O_NONBLOCK is not decoration: the file type is only known *after* the open, so
# without it opening a FIFO (or device node) planted under $GROK_HOME blocks
# until a writer appears — turning a hostile file into a wedged collector tick
# instead of a "no detail" card. It is a no-op for regular files.
_GROK_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _grok_open_verified(path: str, max_size: Optional[int] = None, marker: bool = False, whole: bool = False) -> Optional[int]:
    """Open a trusted Grok file and return a verified fd, or None.

    One place owns the security-critical steps: the trust walk on the path, the
    O_NOFOLLOW|O_NONBLOCK open, and fd-level verification (regular file, current
    owner, single link, optional size cap, containment in $GROK_HOME). With
    `whole=True` the file must also fit in the budget it is charged to — a
    truncated prefix must never be mistaken for the file itself. The caller owns
    closing the fd.
    """
    if _GROK_BUDGET.exhausted() or not grok_trusted_path(path):
        return None
    try:
        fd = os.open(path, _GROK_OPEN_FLAGS)
    except (OSError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink > 1:
            raise OSError("not a regular, owned, single-link file")
        if max_size is not None and st.st_size > max_size:
            raise OSError("file larger than the allowed size")
        if whole:
            allowance = (GROK_MARKER_MAX_TOTAL_BYTES - _GROK_BUDGET.marker_bytes) if marker else _grok_allowance()
            if allowance <= 0 or st.st_size > allowance:
                raise OSError("remaining budget cannot cover the whole file")
        if not _grok_fd_within_home(fd):
            raise OSError("not contained in GROK_HOME")
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return fd


def _grok_allowance() -> int:
    """Bytes still available in this cycle's read budget (<= 0 when spent)."""
    return GROK_CYCLE_MAX_BYTES - _GROK_BUDGET.bytes_read


def _grok_read_at(fd: int, offset: int, length: int, marker: bool = False) -> Optional[bytes]:
    """Read `length` bytes at `offset` from a verified fd.

    Reads are charged to the cycle budget and can never take it over the ceiling.
    `marker=True` charges the separate marker allowance instead, so a tree full of
    large `.cwd` markers cannot starve real session reads.
    """
    if marker:
        allowance = GROK_MARKER_MAX_TOTAL_BYTES - _GROK_BUDGET.marker_bytes
    else:
        allowance = _grok_allowance()
    if allowance <= 0 or length <= 0:
        return None
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        data = os.read(fd, min(length, allowance))
    except (OSError, ValueError):
        return None
    if not data:
        return b""
    if marker:
        _GROK_BUDGET.marker_bytes += len(data)
    elif not _GROK_BUDGET.spend(len(data)):
        return None
    return data


def grok_read_bytes(path: str, max_bytes: int) -> Optional[bytes]:
    """Read a whole trusted Grok file, refusing anything larger than `max_bytes`.

    The opened fd is what gets verified (see _grok_open_verified), so a symlink
    swapped in after the path check cannot redirect the read and a FIFO cannot
    block it. Returns None — never raises — whenever anything is off.
    """
    fd = _grok_open_verified(path, max_size=max_bytes, whole=True)
    if fd is None:
        return None
    try:
        return _grok_read_at(fd, 0, max_bytes)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def grok_read_marker_bytes(path: str) -> Optional[bytes]:
    """Read a whole `.cwd` marker against the marker allowance.

    Whole-file only: a marker read that the remaining allowance would truncate is
    refused, because a truncated marker is a wrong path and would be accepted as a
    breadcrumb or a group match.
    """
    if _GROK_BUDGET.exhausted() or _GROK_BUDGET.marker_bytes >= GROK_MARKER_MAX_TOTAL_BYTES:
        return None
    fd = _grok_open_verified(path, max_size=GROK_CWD_MARKER_MAX_BYTES, marker=True, whole=True)
    if fd is None:
        return None
    try:
        return _grok_read_at(fd, 0, GROK_CWD_MARKER_MAX_BYTES, marker=True)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def grok_read_last_lines(path: str, max_lines: int = 200, chunk_bytes: int = GROK_JSONL_TAIL_BYTES, max_total: int = GROK_JSONL_MAX_LINE_BYTES) -> Optional[bytes]:
    """Read the tail of a trusted JSONL file backwards until `max_lines` lines.

    Chunks are read from the end and each is charged once — no window is re-read —
    so the byte cost is proportional to what is actually needed. A single very
    long line widens the read one chunk at a time up to `max_total`; a line larger
    than that absolute ceiling is dropped (documented limit) rather than parsed
    from a fragment. The returned bytes may start mid-line; callers drop that
    first fragment.
    """
    fd = _grok_open_verified(path)
    if fd is None:
        return None
    try:
        try:
            size = os.fstat(fd).st_size
        except OSError:
            return b""
        parts: List[bytes] = []
        total = 0
        newlines = 0
        pos = size
        while pos > 0 and total < max_total and newlines <= max_lines:
            allowance = _grok_allowance()
            if allowance <= 0:
                break
            # Clamp the WINDOW before computing the offset: this reader walks
            # backwards, so a length-clamped read would hand back the oldest bytes
            # of the window instead of the newest, silently losing the events that
            # decide the card's status.
            take = min(chunk_bytes, pos, max_total - total, allowance)
            if take <= 0:
                break
            data = _grok_read_at(fd, pos - take, take)
            if data is None or not data:
                break
            parts.append(data)
            total += len(data)
            pos -= len(data)
            newlines += data.count(b"\n")
        if not parts:
            return b""
        return b"".join(reversed(parts))
    finally:
        try:
            os.close(fd)
        except OSError:
            pass



def grok_read_json(path: str, max_bytes: int) -> Any:
    """Parsed JSON from a trusted Grok file, or None. Never raises."""
    raw = grok_read_bytes(path, max_bytes)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None


def grok_read_json_cached(path: str, max_bytes: int) -> Any:
    """grok_read_json memoized per (path, inode, mtime, size, cap) for one cycle."""
    if _GROK_BUDGET.exhausted():
        return None
    try:
        st = os.stat(path)
        key = (path, int(st.st_ino), int(st.st_mtime_ns), int(st.st_ctime_ns), int(st.st_size), int(max_bytes))
    except (OSError, ValueError):
        return None
    if _GROK_BUDGET.has(key):
        return _GROK_BUDGET.get(key)
    return _GROK_BUDGET.store(key, grok_read_json(path, max_bytes))


def grok_is_regular_file(path: str) -> bool:
    """True for a trusted regular, single-link file — never follows a symlink.

    Used wherever the code decides "this is a session / this stream exists":
    os.path.isfile()/exists() follow symlinks and would accept a planted link
    even though the guarded reader then refuses its contents.
    """
    if _GROK_BUDGET.exhausted() or not grok_trusted_path(path):
        return False
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def grok_listdir(path: str, max_entries: int = GROK_MAX_GROUP_ENTRIES) -> List[str]:
    """Bounded, trusted, sorted directory listing; [] when untrusted or over budget."""
    if _GROK_BUDGET.exhausted() or not grok_trusted_path(path):
        return []
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return []
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or not _GROK_BUDGET.spend(0, 1):
        return []
    names: List[str] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                names.append(entry.name)
                if len(names) >= max_entries:
                    break
    except (OSError, ValueError):
        return []
    return sorted(names)


def grok_session_is_subagent(session_dir: str) -> bool:
    """Skip forked subagent sessions so the parent card is not counted twice."""
    summary = grok_read_json_cached(os.path.join(session_dir, "summary.json"), GROK_SUMMARY_MAX_BYTES)
    if not isinstance(summary, dict):
        return False
    # session_kind is the reliable flag (agent_name is the role, e.g. general-purpose).
    for key in ("session_kind", "agent_name", "session_relationship"):
        val = str(summary.get(key) or "")
        if val.startswith("subagent"):
            return True
    return False


def grok_sessions_root() -> str:
    """Grok's sessions root ($GROK_HOME/sessions)."""
    return os.path.join(grok_home(), "sessions")


def grok_group_dirs() -> List[str]:
    """Trusted group directories under the sessions root, bounded and sorted.

    Built once per fetch cycle: resolving several cards against one session tree
    would otherwise re-list and re-stat every group directory each time and burn
    the op budget.
    """
    if _GROK_BUDGET.group_dirs is not None:
        return _GROK_BUDGET.group_dirs
    root = grok_sessions_root()
    groups: List[str] = []
    for name in grok_listdir(root, GROK_MAX_GROUP_DIRS):
        if len(groups) >= GROK_MAX_GROUP_DIRS:
            break
        path = os.path.join(root, name)
        if grok_trusted_path(path):
            groups.append(path)
    _GROK_BUDGET.group_dirs = groups
    return groups


def grok_group_dir_for_name(encoded: str) -> Optional[str]:
    """Map an encoded cwd name to a real group directory, refusing dot components.

    quote(cwd, safe="") deliberately leaves '.' unescaped, so a roster cwd of
    '.' or '..' would otherwise resolve to the sessions root or to GROK_HOME
    itself. The candidate must be a direct child of the sessions root.
    """
    if not encoded or encoded in (".", "..") or "/" in encoded or os.sep in encoded:
        return None
    root = os.path.abspath(grok_sessions_root())
    candidate = os.path.abspath(os.path.join(root, encoded))
    if os.path.dirname(candidate) != root:
        return None
    return candidate


def grok_session_dir_for_id(cwd: str, session_id: str) -> Optional[str]:
    """Resolve a live session's directory from its (uuid) session id.

    The id — not the group name — is the reliable key. The encoded-cwd fast path
    short-circuits when it works; the bounded group scan then finds the session
    whatever Grok called the group, including the slug+hash group it uses when
    the encoded cwd exceeds 255 bytes (documented layout), and regardless of
    whether our encoder matches Grok's byte for byte.
    """
    if not GROK_SESSION_ID_RE.match(session_id or ""):
        return None
    root = grok_sessions_root()
    if cwd:
        encoded_names: List[str] = []
        try:
            for value in (cwd, os.path.realpath(cwd)):
                encoded = quote(value, safe="")
                if encoded not in encoded_names:
                    encoded_names.append(encoded)
        except (OSError, ValueError):
            encoded_names = []
        for encoded in encoded_names:
            group = grok_group_dir_for_name(encoded)
            if not group:
                continue
            candidate = os.path.join(group, session_id)
            if grok_is_regular_file(os.path.join(candidate, "summary.json")):
                return candidate
    for group in grok_group_dirs():
        candidate = os.path.join(group, session_id)
        if grok_is_regular_file(os.path.join(candidate, "summary.json")):
            return candidate
    return None


def grok_active_session_for_pid(pid: int) -> Optional[str]:
    """Map a live grok PID to its session directory via active_sessions.json.

    The roster is read-only input: a row only counts when its pid is this live
    process, its session_id passes the uuid check, and the resolved directory is
    inside the trusted tree. Anything else is dropped rather than guessed at.
    """
    if pid <= 1 or not os.path.exists(f"/proc/{pid}"):
        return None
    rows = grok_read_json_cached(os.path.join(grok_home(), "active_sessions.json"), GROK_ROSTER_MAX_BYTES)
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            row_pid = int(row.get("pid"))
        except (TypeError, ValueError, OverflowError):
            continue
        if row_pid != pid:
            continue
        session_id = str(row.get("session_id") or "")
        cwd = str(row.get("cwd") or "")
        if not GROK_SESSION_ID_RE.match(session_id):
            continue
        session_dir = grok_session_dir_for_id(cwd, session_id)
        if session_dir and not grok_session_is_subagent(session_dir):
            return session_dir
    return None


def grok_summary_mtime(session_dir: str) -> float:
    """summary.json mtime of a session dir (0.0 when it is not a plain file).

    lstat, not getmtime: a symlinked summary.json that every reader refuses must
    not contribute its target's mtime to the newest-first ordering.
    """
    try:
        st = os.lstat(os.path.join(session_dir, "summary.json"))
    except (OSError, ValueError):
        return 0.0
    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
        return 0.0
    return float(st.st_mtime)


def grok_group_cwd_marker(group_dir: str) -> Optional[str]:
    """The absolute cwd recorded in a group's `.cwd` marker file, or None.

    Grok writes this marker only for groups whose name is a slug+hash, i.e. when
    the URL-encoded path exceeded 255 bytes. The file's contents are untrusted
    text — a local file can hold anything — so they are flattened, redacted and
    capped before use, and only a value that actually looks like an absolute path
    is accepted. The marker is never used to build a path, and the mapping is
    memoized for the cycle, which is also what keeps a 512-group scan inside the
    op budget.
    """
    if group_dir in _GROK_BUDGET.group_cwd:
        return _GROK_BUDGET.group_cwd[group_dir]
    recorded = None
    marker = grok_read_marker_bytes(os.path.join(group_dir, ".cwd"))
    if marker is not None:
        text = grok_clean_detail(marker.decode("utf-8", errors="replace"), GROK_CWD_MARKER_MAX_BYTES)
        # A marker is only ever a path: absolute, with no space or control byte
        # left in it after cleaning. Anything else is dropped rather than rendered
        # as "the repo path".
        if text.startswith("/") and " " not in text:
            recorded = text
    _GROK_BUDGET.group_cwd[group_dir] = recorded
    return recorded


def grok_group_cwd(group_dir: str) -> str:
    """The working directory a session group belongs to.

    Prefers the `.cwd` marker and falls back to the decoded group name, which is
    how Grok names groups for ordinary (short) paths.
    """
    recorded = grok_group_cwd_marker(group_dir)
    if recorded:
        return recorded
    try:
        return unquote(os.path.basename(group_dir))
    except Exception:
        return ""


def grok_sessions_for_cwd(cwd: str) -> List[str]:
    """Session directories for a cwd, best-first (encoded group, then markers).

    Two group-naming schemes are covered: the URL-encoded cwd (literal and
    realpath forms) and slug+hash groups whose `.cwd` marker names this cwd. The
    encoded name is deterministic, so it is tried first and, when it yields a
    session, the marker scan is skipped entirely: marker contents are
    attacker-controllable text, and a group that merely claims this cwd must never
    outrank the real one or spend the budget its summary/events reads need. For the
    same reason the number of marker-matched groups enumerated per cwd is capped.
    """
    if not cwd:
        return []
    wanted = [cwd]
    try:
        real = os.path.realpath(cwd)
    except (OSError, ValueError):
        real = ""
    if real and real not in wanted:
        wanted.append(real)

    fast_folders: List[str] = []
    for value in wanted:
        try:
            folder = grok_group_dir_for_name(quote(value, safe=""))
        except (OSError, ValueError):
            folder = None
        if folder and folder not in fast_folders:
            fast_folders.append(folder)

    found: List[str] = []
    for folder in fast_folders:
        for name in grok_listdir(folder):
            path = os.path.join(folder, name)
            if grok_is_regular_file(os.path.join(path, "summary.json")) and not grok_session_is_subagent(path):
                found.append(path)
    if found:
        found.sort(key=grok_summary_mtime, reverse=True)
        return found

    matched = 0
    for group in grok_group_dirs():
        if group in fast_folders or matched >= GROK_MAX_MATCHED_GROUPS:
            continue
        if grok_group_cwd(group) not in wanted:
            continue
        matched += 1
        group_found: List[str] = []
        for name in grok_listdir(group):
            path = os.path.join(group, name)
            if grok_is_regular_file(os.path.join(path, "summary.json")) and not grok_session_is_subagent(path):
                group_found.append(path)
        group_found.sort(key=grok_summary_mtime, reverse=True)
        found.extend(group_found)
    return found


def _iter_jsonl_tail(path: str, max_lines: int = 200) -> List[Dict[str, Any]]:
    """Parsed events from the tail of a trusted Grok JSONL file (newest kept).

    The tail is read backwards in chunks until `max_lines` lines are covered (or
    the 512 KiB ceiling is reached), so a decisive event larger than a single
    chunk is still parsed rather than truncated into a fragment. Lines are parsed
    whole and only the newest `max_lines` non-empty ones are returned.
    """
    raw = grok_read_last_lines(path, max_lines=max_lines)
    if not raw:
        return []
    lines = [line.strip() for line in raw.decode("utf-8", errors="replace").split("\n")]
    entries: List[Dict[str, Any]] = []
    for line in [line for line in lines if line][-max_lines:]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def grok_clean_detail(text: str, max_len: int = 200) -> str:
    """Redact, flatten and cap an event-supplied detail line for the UI.

    events.jsonl is agent-written content like every other detail source, so it
    takes the same secret-redaction pass every other detail does — a tool name
    is normally harmless, but nothing here may leak a token into an always-on
    bar just because it arrived in the wrong field. C0/C1 control bytes are
    dropped as well: clean_ansi() only understands CSI/Fe sequences, so an OSC or
    a bare BEL would otherwise survive into a Text element.
    """
    cleaned = clean_ansi(str(text or ""))
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", cleaned)
    return " ".join(redact_secrets(cleaned).split())[:max_len]


def grok_phase_detail(phase: str) -> str:
    """Human-readable detail text for a Grok phase name."""
    if phase == "waiting_for_model":
        return "Thinking…"
    if phase == "streaming_reasoning":
        return "Reasoning…"
    if phase == "tool_execution":
        return "Running tool"
    return "Generating response…"


def extract_grok_task_from_session(
    session_dir: str,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], bool]:
    """Read a Grok session directory.

    Returns (latest_user_prompt, detail_text, model_name, status_override, has_question).
    Status comes from a bounded tail of events.jsonl; the title/model from summary.json
    plus the last real user line in chat_history.jsonl.
    """
    if not session_dir or not grok_trusted_path(session_dir):
        return None, None, None, None, False

    model_name = None
    latest_user_prompt = None
    last_turn_summary = None
    summary_path = os.path.join(session_dir, "summary.json")
    summary = grok_read_json_cached(summary_path, GROK_SUMMARY_MAX_BYTES)
    if isinstance(summary, dict):
        # The model id is file-supplied text like every other field, so it takes
        # the same flatten/redact/cap pass before it reaches the model chip.
        model_name = grok_clean_detail(clean_model_name(summary.get("current_model_id") or ""), 120)
        title = summary.get("generated_title") or summary.get("session_summary") or ""
        if isinstance(title, str) and title.strip():
            latest_user_prompt = grok_clean_detail(clean_user_prompt(title), 300)
        turn = summary.get("last_turn_summary")
        if isinstance(turn, str) and turn.strip():
            last_turn_summary = grok_clean_detail(extract_first_line(turn), 200)

    history_path = os.path.join(session_dir, "chat_history.jsonl")
    if grok_is_regular_file(history_path):
        for entry in reversed(_iter_jsonl_tail(history_path)):
            if entry.get("type") != "user":
                continue
            if entry.get("synthetic_reason"):
                continue
            content = entry.get("content")
            raw = content if isinstance(content, str) else ""
            cleaned = grok_clean_detail(clean_user_prompt(raw), MAX_PROMPT_CHARS)
            if cleaned and not is_system_wrapper(cleaned):
                latest_user_prompt = cleaned
                break

    # Event state is folded in log order so the LAST event wins: a tool that
    # started after a permission prompt means "working" (not "waiting"), and a
    # stale permission_prompt phase cannot mask the tool that is running now.
    pending_tool: Optional[str] = None
    pending_permission: Optional[str] = None
    phase: Optional[str] = None
    turn_open = False
    turn_finished = False
    events_path = os.path.join(session_dir, "events.jsonl")
    if grok_is_regular_file(events_path):
        for entry in _iter_jsonl_tail(events_path):
            et = entry.get("type")
            if et == "turn_started":
                turn_open = True
                turn_finished = False
                pending_tool = None
                pending_permission = None
                phase = "waiting_for_model"
            elif et == "turn_ended":
                turn_open = False
                turn_finished = True
                pending_tool = None
                pending_permission = None
                phase = None
            elif et == "phase_changed":
                value = entry.get("phase")
                if isinstance(value, str):
                    phase = value
            elif et == "tool_started":
                pending_tool = str(entry.get("tool_name") or "tool")
                pending_permission = None
                phase = "tool_execution"
            elif et == "tool_completed":
                pending_tool = None
            elif et == "permission_requested":
                pending_permission = str(entry.get("tool_name") or "tool")
                pending_tool = None
            elif et == "permission_resolved":
                pending_permission = None
                # Auto-allowed asks leave phase=permission_prompt behind; the
                # turn is no longer waiting on the user.
                if phase in GROK_WAITING_PHASES:
                    phase = "tool_execution" if pending_tool else "waiting_for_model"

    status_override = None
    detail = None
    has_question = False
    if pending_permission:
        status_override = "waiting"
        detail = grok_clean_detail(f"Permission: {pending_permission}")
        has_question = True
    elif pending_tool in GROK_WAITING_TOOLS:
        status_override = "waiting"
        detail = "Waiting for input"
        has_question = True
    elif pending_tool:
        status_override = "working"
        detail = grok_clean_detail(f"Running: {pending_tool}")
    elif phase in GROK_WORKING_PHASES:
        status_override = "working"
        detail = grok_phase_detail(phase)
    elif phase in GROK_WAITING_PHASES:
        status_override = "waiting"
        detail = "Waiting for permission"
        has_question = True
    elif turn_open:
        status_override = "working"
        detail = "Thinking…"
    elif turn_finished:
        status_override = "completed"
        detail = last_turn_summary or "Task completed"
    else:
        status_override = "idle"
        detail = "Ready for prompt"

    return latest_user_prompt, detail, model_name or "", status_override, has_question


def is_valid_agent_process(pid: int) -> bool:
    """Validate that a PID actually corresponds to an active AI agent process before signaling."""
    if pid <= 1:
        return False
    info = get_process_info(pid)
    if not info or not info.get("cmd"):
        return False
    cmd = info["cmd"]
    tokens = cmd.split()
    first = os.path.basename(tokens[0]) if tokens else ""
    argv = info.get("argv")
    if first == "grok" and not is_grok_helper_process(cmd, argv):
        return True
    if first in ("omp", "pi", "claude", "codex", "opencode", "cline", "cursor", "agy") and not is_claude_helper_process(cmd, argv):
        return True
    if first in ("python", "python3") and is_hermes_cli_process(cmd):
        return True
    if "/Hermes" in cmd or "Hermes" in cmd:
        return True
    return False


def get_process_info(pid: int) -> Optional[Dict[str, Any]]:
    """Robustly parse /proc/<pid>/stat and cmdline."""
    try:
        proc_dir = f"/proc/{pid}"
        if not os.path.exists(proc_dir):
            return None
        with open(f"{proc_dir}/stat", "r") as sf:
            content = sf.read()
            last_paren = content.rfind(")")
            if last_paren == -1:
                return None
            fields = content[last_paren + 1:].split()
            state = fields[0]
            ppid = int(fields[1])
            tty_nr = int(fields[4])
        with open(f"{proc_dir}/cmdline", "rb") as cf:
            raw_cmdline = cf.read().decode("utf-8", errors="replace")
        # Exact argv elements (cmdline is NUL-separated). Empty string = zombie.
        # `argv` preserves boundaries; `cmd` keeps the legacy space-joined form
        # for substring checks (argv elements may themselves contain spaces).
        argv = [a for a in raw_cmdline.split("\x00") if a != ""]
        cmd = " ".join(argv).strip()
        cwd = os.path.realpath(f"{proc_dir}/cwd")
        return {"pid": pid, "ppid": ppid, "tty": tty_nr, "state": state, "cmd": cmd, "cwd": cwd, "argv": argv}
    except Exception:
        return None


def get_herdr_server_pids() -> List[int]:
    """Find PID(s) of running Herdr server and client instances."""
    pids = []
    for p in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(p))
            info = get_process_info(pid)
            if info and ("herdr server" in info["cmd"] or info["cmd"] == "herdr" or "herdr --session" in info["cmd"]):
                pids.append(pid)
        except Exception:
            continue
    return pids


def get_process_ancestors(pid: int, max_depth: int = 20) -> List[Dict[str, Any]]:
    """Return ordered list of ancestor process info dictionaries up to PID 1."""
    ancestors = []
    curr = pid
    visited = set()
    depth = 0
    while curr > 1 and curr not in visited and depth < max_depth:
        visited.add(curr)
        depth += 1
        info = get_process_info(curr)
        if not info or info["ppid"] <= 0:
            break
        ancestors.append(info)
        curr = info["ppid"]
    return ancestors


def get_process_start_time(pid: int) -> float:
    """Extract process start time in epoch seconds."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read().split()
        starttime_ticks = int(stat[21])
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime"):
                    btime = int(line.split()[1])
                    break
        clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        return btime + (starttime_ticks / clock_ticks)
    except Exception:
        return 0.0


def get_process_open_session(pid: int) -> Optional[str]:
    """Inspect open file descriptors of a process for active session files."""
    try:
        for fd in glob.glob(f"/proc/{pid}/fd/*"):
            try:
                target = os.readlink(fd)
                if ".jsonl" in target and os.path.exists(target):
                    return target
            except Exception:
                continue
    except Exception:
        pass
    return None


# Only a status from the known vocabulary carrying a fresh timestamp is trusted,
# so a malformed, foreign or stale file can never reach the UI. Freshness matters
# because a session killed with SIGKILL never fires SessionEnd, and its last
# "working" would otherwise stick around forever. The detail line is
# hook-supplied free text (a tool name, a permission prompt), so it runs through
# the same first-line and secret-redaction pass as every other agent detail.
def read_claude_hook_status(pid: int) -> Optional[Dict[str, str]]:
    """Read the live status Claude Code's own hooks wrote for this PID."""
    path = os.path.join(CLAUDE_HOOK_STATUS_DIR, f"{pid}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if status not in CLAUDE_HOOK_STATUSES:
        return None
    updated = data.get("updated")
    if not isinstance(updated, (int, float)) or (time.time() - updated) > CLAUDE_HOOK_STATUS_MAX_AGE:
        return None
    detail = data.get("detail")
    return {
        "status": status,
        "detail": extract_first_line(clean_ansi(detail)) if isinstance(detail, str) else "",
    }


def find_session_for_process(agent_type: str, pid: int, cwd: str, claimed_sessions: Set[str], argv: Optional[List[str]] = None) -> Optional[str]:
    """Find active or recent session file strictly belonging to this specific process."""
    if agent_type == "grok":
        # 1. The live roster is authoritative: it is keyed by the session's own
        #    pid, so concurrent sessions in one cwd stay distinct, and its cwd is
        #    Grok's view of the session directory rather than /proc's.
        active = grok_active_session_for_pid(pid)
        if active:
            return active if active not in claimed_sessions else None
        # 2. Fallback for a session the roster has not caught up with. Skip it
        #    entirely when the invocation passed `--cwd`: the process cwd is then
        #    not the session cwd, and a wrong card is worse than no card.
        if grok_argv_has_cwd_override(argv):
            return None
        p_start = get_process_start_time(pid)
        for session_dir in grok_sessions_for_cwd(cwd):
            if session_dir in claimed_sessions or grok_session_is_subagent(session_dir):
                continue
            mtime = grok_summary_mtime(session_dir)
            if p_start > 0 and mtime >= (p_start - 5.0):
                return session_dir
        return None

    if agent_type == "omp":
        # 1. Check PTS terminal session mapping in ~/.omp/agent/terminal-sessions/pts-<N>
        try:
            for fd in glob.glob(f"/proc/{pid}/fd/*"):
                try:
                    target = os.readlink(fd)
                    if target.startswith("/dev/pts/"):
                        pts_num = target.split("/")[-1]
                        pts_file = os.path.expanduser(f"~/.omp/agent/terminal-sessions/pts-{pts_num}")
                        if os.path.exists(pts_file):
                            with open(pts_file, "r", encoding="utf-8") as pf:
                                lines = [line.strip() for line in pf if line.strip()]
                                if len(lines) >= 2:
                                    sess_cand = lines[1]
                                    if sess_cand not in claimed_sessions:
                                        return sess_cand
                except Exception:
                    continue
        except Exception:
            pass

    # 2. Direct open file descriptor
    open_s = get_process_open_session(pid)
    if open_s and open_s not in claimed_sessions:
        return open_s

    # 3. If no open session, check if there is an unclaimed session modified around/after process started
    p_start = get_process_start_time(pid)
    if agent_type == "omp" and os.path.exists(OMP_SESSIONS_DIR):
        folder_part = os.path.basename(cwd.rstrip("/")) if cwd else ""
        if folder_part and folder_part not in ("tmp", "~"):
            matches = glob.glob(os.path.join(OMP_SESSIONS_DIR, f"*{folder_part}*", "*.jsonl"))
        else:
            matches = glob.glob(os.path.join(OMP_SESSIONS_DIR, "*-tmp*", "*.jsonl"))
        if matches:
            matches.sort(key=os.path.getmtime, reverse=True)
            for m in matches:
                if m not in claimed_sessions:
                    mtime = os.path.getmtime(m)
                    if p_start > 0 and mtime >= (p_start - 5.0):
                        return m
    return None


def match_hypr_client_for_terminal(ancestor_pids: List[int], cwd: str, agent_type: str) -> Optional[Dict[str, Any]]:
    """Find the exact Hyprland client window associated with a terminal process."""
    clients = get_hypr_clients()
    candidates = [c for c in clients if c.get("pid") in ancestor_pids]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    folder_part = os.path.basename(cwd.rstrip("/")) if cwd else ""
    for c in candidates:
        t = c.get("title", "").lower()
        if "omarchy:" in t or "herdr" in t:
            continue
        if folder_part and folder_part in t:
            return c
        if "π" in t or (agent_type and agent_type.lower() in t):
            return c

    for c in candidates:
        t = c.get("title", "").lower()
        if "omarchy:" not in t and "herdr" not in t:
            return c
    return candidates[0]


def find_latest_session_for_cwd(agent_type: str, cwd: str, claimed_sessions: Optional[Set[str]] = None) -> Optional[str]:
    """Find the most recent session file matching a working directory that is not already claimed."""
    claimed = claimed_sessions or set()
    if agent_type == "grok":
        try:
            for session_dir in grok_sessions_for_cwd(cwd):
                if session_dir not in claimed and not grok_session_is_subagent(session_dir):
                    return session_dir
        except Exception:
            pass
        return None
    if agent_type == "omp" and os.path.exists(OMP_SESSIONS_DIR):
        try:
            folder_part = os.path.basename(cwd.rstrip("/")) if cwd else ""
            if folder_part and folder_part not in ("tmp", "~"):
                matches = glob.glob(os.path.join(OMP_SESSIONS_DIR, f"*{folder_part}*", "*.jsonl"))
            else:
                matches = glob.glob(os.path.join(OMP_SESSIONS_DIR, "*-tmp*", "*.jsonl"))
            if not matches:
                matches = glob.glob(os.path.join(OMP_SESSIONS_DIR, "*", "*.jsonl"))
            if matches:
                matches.sort(key=os.path.getmtime, reverse=True)
                for m in matches:
                    if m not in claimed:
                        return m
        except Exception:
            pass
    return None


def extract_first_line(text: str, max_len: int = 140) -> str:
    """Extract the first meaningful non-empty line of the assistant response."""
    if not text:
        return ""
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        # Strip leading markdown headers (#, ##, ###)
        line = re.sub(r"^#+\s*", "", line).strip()
        # Strip markdown bolding (**text**), italics (*text*), code (`code`)
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        line = re.sub(r"\*([^*]+)\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        line = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", clean_ansi(line)).split())
        if not line:
            continue
        line = redact_secrets(line)
        if len(line) > max_len:
            return line[:max_len - 1].rstrip() + "…"
        return line
    return ""


MAX_PROMPT_CHARS = 400  # display cap for prompt/title text (card title, bar headline)


def clean_user_prompt(text: str) -> str:
    """Clean user prompt text by stripping system wrappers, XML tags, secrets, and extra whitespace."""
    if not text:
        return ""
    cleaned = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
    cleaned = re.sub(r"<system-directive>.*?</system-directive>", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"<[^>]+>", "", cleaned).strip()
    cleaned = re.sub(r"^#+\s*", "", cleaned).strip()
    # Untrusted text (session transcripts, terminal titles): drop escape sequences
    # and control bytes before redaction — an embedded control byte both reaches
    # the UI and splits a credential so the token regex no longer matches it.
    cleaned = clean_ansi(cleaned)
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", cleaned)
    cleaned = redact_secrets(cleaned)
    # Display text only, but it ends up in card titles and the bar headline, so cap
    # it: an unbounded prompt would otherwise inflate the whole status payload.
    return " ".join(cleaned.split())[:MAX_PROMPT_CHARS]


def is_system_wrapper(text: str) -> bool:
    """Check if a prompt is an internal system directive or retry wrapper rather than a human prompt."""
    if not text:
        return True
    t = text.strip()
    if t.startswith("[System:") or t.startswith("[system:") or t.startswith("<system-directive>"):
        return True
    return False


def extract_omp_task_from_session(
    session_path: str,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], bool]:
    """
    Extract latest user prompt, latest activity/detail, model name, status override, and has_question flag from an OMP session .jsonl file.
    Returns (latest_user_prompt, detail_text, model_name, status_override, has_question).
    """
    if not session_path or not os.path.exists(session_path):
        return None, None, None, None, False
    try:
        latest_user_prompt = None
        model_name = None
        last_assistant_text = None
        pending_tool = None
        pending_ask_question = None
        session_exited = False

        file_size = os.path.getsize(session_path)
        with open(session_path, "r", encoding="utf-8", errors="replace") as f:
            tail_bytes = 65536  # Bounded 64KB tail read
            if file_size > tail_bytes:
                f.seek(file_size - tail_bytes)
                f.readline(4096)  # discard partial first line

            lines_read = 0
            while lines_read < 100:
                line = f.readline(8192)  # Bounded 8KB per line
                if not line:
                    break
                lines_read += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    msg_type = entry.get("type")

                    if entry.get("type") == "model_change" and entry.get("model"):
                        model_name = entry["model"]
                    elif "model" in entry and entry["model"]:
                        model_name = entry["model"]
                    elif "data" in entry and isinstance(entry["data"], dict):
                        if entry["data"].get("model"):
                            model_name = entry["data"]["model"]
                        elif entry["data"].get("modelId"):
                            model_name = entry["data"]["modelId"]
                    if msg_type == "message":
                        msg = entry.get("message", {})
                        role = msg.get("role")
                        if "model" in msg and msg["model"]:
                            model_name = msg["model"]

                        if role == "user":
                            content = msg.get("content")
                            raw_txt = ""
                            if isinstance(content, list):
                                parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")]
                                raw_txt = "".join(parts).strip()
                            elif isinstance(content, str):
                                raw_txt = content.strip()

                            cleaned = clean_user_prompt(raw_txt)
                            if cleaned and not is_system_wrapper(cleaned):
                                latest_user_prompt = cleaned
                        elif role == "assistant":
                            content = msg.get("content", [])
                            if isinstance(content, list):
                                for item in content:
                                    if isinstance(item, dict):
                                        if item.get("type") == "text" and item.get("text"):
                                            last_assistant_text = item["text"]
                                            pending_tool = None
                                            pending_ask_question = None
                                        elif item.get("type") == "toolCall":
                                            t_name = item.get("name") or "tool"
                                            t_intent = item.get("intent") or (item.get("arguments") or item.get("args") or {}).get("i") or t_name
                                            pending_tool = (t_name, t_intent)
                                            if t_name == "ask":
                                                args = item.get("arguments") or item.get("args") or {}
                                                questions = args.get("questions") or []
                                                if questions and isinstance(questions, list) and isinstance(questions[0], dict):
                                                    pending_ask_question = questions[0].get("question") or questions[0].get("header")

                        elif role == "toolResult":
                            pending_tool = None
                            pending_ask_question = None

                    elif msg_type == "custom":
                        c_type = entry.get("customType")
                        if c_type == "tool_execution_start":
                            data = entry.get("data", {})
                            t_name = data.get("toolName") or "tool"
                            t_intent = data.get("intent") or t_name
                            pending_tool = (t_name, t_intent)
                            if t_name == "ask":
                                q = data.get("question")
                                if q:
                                    pending_ask_question = q
                        elif c_type == "session_exit":
                            session_exited = True
                            pending_tool = None
                            pending_ask_question = None
                except Exception:
                    continue

        # If user prompt or model was earlier than the tail seek, quickly read from head with bounded lines
        if (not latest_user_prompt or not model_name) and file_size > 65536:
            try:
                with open(session_path, "r", encoding="utf-8", errors="replace") as f:
                    for _ in range(25):
                        line = f.readline(4096)
                        if not line:
                            break
                        try:
                            entry = json.loads(line.strip())
                            if not model_name:
                                if entry.get("type") == "model_change" and entry.get("model"):
                                    model_name = entry["model"]
                                elif "model" in entry and entry["model"]:
                                    model_name = entry["model"]
                            if not latest_user_prompt and entry.get("type") == "message":
                                msg = entry.get("message", {})
                                if msg.get("role") == "user":
                                    content = msg.get("content")
                                    raw_txt = ""
                                    if isinstance(content, list):
                                        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")]
                                        raw_txt = "".join(parts).strip()
                                    elif isinstance(content, str):
                                        raw_txt = content.strip()
                                    cleaned = clean_user_prompt(raw_txt)
                                    if cleaned and not is_system_wrapper(cleaned):
                                        latest_user_prompt = cleaned
                        except Exception:
                            continue
            except Exception:
                pass
        status_override = None
        detail = None
        has_question = False

        if session_exited:
            status_override = "completed"
        elif pending_ask_question:
            status_override = "waiting"
            detail = f"❓ {pending_ask_question}"
            has_question = True
        elif pending_tool:
            status_override = "working"
            t_name, t_intent = pending_tool
            detail = f"Running: {t_intent}" if t_intent else f"Running tool: {t_name}"
        elif last_assistant_text:
            detail = extract_first_line(last_assistant_text)
            has_question = "?" in (detail[-40:] if detail else "")
            if has_question:
                status_override = "waiting"
            else:
                status_override = "completed"
        eff_model = clean_model_name(model_name) if model_name else get_omp_default_model()
        return latest_user_prompt, detail, eff_model, status_override, has_question
    except Exception:
        return None, None, None, None, False


def get_all_hermes_dbs() -> List[Tuple[str, str]]:
    """Discover default and profile-specific Hermes SQLite databases."""
    dbs = []
    base_db = os.path.expanduser("~/.hermes/state.db")
    if os.path.exists(base_db):
        dbs.append((base_db, "Default"))
    for p in glob.glob(os.path.expanduser("~/.hermes/profiles/*/state.db")):
        profile_name = os.path.basename(os.path.dirname(p))
        dbs.append((p, profile_name))
    return dbs


def extract_hermes_session_info(
    source_preference: Optional[str] = None,
    specific_session_id: Optional[str] = None,
    min_start_time: Optional[float] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], bool]:
    """Extract prompt, model, provider, profile, message detail, and active status for Hermes.

    Scans default and profile state databases, inspects active turn leases, and evaluates
    message stream state to accurately determine working, waiting, or idle status.
    """
    now = time.time()
    all_dbs = get_all_hermes_dbs()
    if not all_dbs:
        return None, None, None, None, None, None, False

    candidates = []

    for db_path, db_profile in all_dbs:
        try:
            db_uri = f"file:{os.path.abspath(db_path)}?mode=ro"
            conn = sqlite3.connect(db_uri, uri=True, timeout=0.5)
            cur = conn.cursor()

            # Find active unexpired turn leases with alive holder PIDs
            active_leases: Dict[str, Dict[str, Any]] = {}
            try:
                cur.execute("SELECT conversation_id, holder, acquired_at, expires_at FROM session_turn_leases LIMIT 64;")
                for cid, holder, acq, exp in cur.fetchall():
                    if exp and float(exp) > now:
                        m = re.search(r"pid=(\d+)", holder or "")
                        pid = int(m.group(1)) if m else None
                        pid_alive = os.path.exists(f"/proc/{pid}") if pid else True
                        if pid_alive:
                            active_leases[cid] = {"holder": holder, "pid": pid, "expires_at": float(exp)}
            except Exception:
                pass

            # Query candidate sessions
            if specific_session_id:
                cur.execute(
                    "SELECT id, source, title, model, billing_provider, profile_name, last_activity_at FROM sessions WHERE id = ? LIMIT 1;",
                    (specific_session_id,)
                )
            elif source_preference:
                cur.execute(
                    "SELECT id, source, title, model, billing_provider, profile_name, last_activity_at FROM sessions WHERE source = ? ORDER BY last_activity_at DESC LIMIT 5;",
                    (source_preference,)
                )
            else:
                cur.execute(
                    "SELECT id, source, title, model, billing_provider, profile_name, last_activity_at FROM sessions ORDER BY last_activity_at DESC LIMIT 5;"
                )

            session_rows = cur.fetchall()
            for s_row in session_rows:
                s_id, s_src, s_title, s_model, s_prov, s_prof, s_active = s_row
                is_lease_active = bool(s_id in active_leases)
                candidates.append({
                    "db_path": db_path,
                    "profile": s_prof or db_profile,
                    "session_id": s_id,
                    "source": s_src,
                    "title": s_title,
                    "model": s_model,
                    "provider": s_prov,
                    "last_active": float(s_active or 0),
                    "is_lease_active": is_lease_active,
                    "lease_info": active_leases.get(s_id),
                })
            conn.close()
        except Exception:
            continue

    if not candidates:
        return None, None, None, None, None, None, False

    # If source_preference is given, strictly filter to matching source if any exist
    if source_preference:
        matching = [c for c in candidates if c.get("source") == source_preference]
        if matching:
            candidates = matching

    # Sort candidates: active leases first among matching, then most recent last_active
    def sort_key(c: Dict[str, Any]) -> Tuple[int, float]:
        lease_score = 1 if c["is_lease_active"] else 0
        return (lease_score, c["last_active"])

    candidates.sort(key=sort_key, reverse=True)
    best = candidates[0]

    # If the candidate session is older than the running process start time (or >4h old without an active lease),
    # then this running Hermes instance is a fresh instance with no active prompt yet.
    is_session_fresh = True
    if not best["is_lease_active"]:
        if min_start_time and best["last_active"] < (min_start_time - 30.0):
            is_session_fresh = False
        elif (now - best["last_active"]) > 14400.0:
            is_session_fresh = False

    if not is_session_fresh:
        return (
            None,
            clean_model_name(best.get("model") or "ox-alpha-free"),
            best.get("provider") or "",
            best.get("profile") or "Default",
            "Ready for prompt",
            "idle",
            False,
        )
    # Open the winning DB and session to extract detailed messages
    try:
        db_uri = f"file:{os.path.abspath(best['db_path'])}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True, timeout=0.3)
        cur = conn.cursor()
        session_id = best["session_id"]

        # Latest human prompt (bounded length & limited rows)
        latest_user_prompt = None
        cur.execute(
            "SELECT substr(content, 1, 2048), display_kind FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT 5;",
            (session_id,)
        )
        for u_content, u_dk in cur.fetchall():
            if u_dk in ("hidden", "auto_continue", "model_switch"):
                continue
            if u_content:
                cleaned_p = clean_user_prompt(u_content)
                if cleaned_p and not is_system_wrapper(cleaned_p):
                    latest_user_prompt = cleaned_p
                    break

        # Latest assistant response / tool status (bounded chunk)
        cur.execute(
            "SELECT role, substr(content, 1, 2048), tool_name, substr(tool_calls, 1, 2048), finish_reason FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1;",
            (session_id,)
        )
        msg_row = cur.fetchone()

        detail = None
        status = "idle"
        has_question = False

        is_active = best["is_lease_active"]
        # Fallback: if last activity was within 10s and any hermes process is active
        if not is_active and (now - best["last_active"] < 10.0):
            is_active = True

        if msg_row:
            role, content, tool_name, tool_calls, finish_reason = msg_row
            if role == "assistant":
                if tool_calls:
                    try:
                        tc = json.loads(tool_calls)
                        if tc and isinstance(tc, list):
                            fn = tc[0].get("function", {})
                            fname = fn.get("name") or "tool"
                            detail = f"Running: {fname}"
                        else:
                            detail = "Running tool"
                    except Exception:
                        detail = "Running tool"
                    status = "working"
                elif content:
                    first_line = extract_first_line(content)
                    has_question = "?" in (first_line[-40:] if first_line else "")
                    if is_active:
                        status = "working"
                        detail = first_line or "Generating response…"
                    elif has_question:
                        status = "waiting"
                        detail = first_line
                    else:
                        status = "completed"
                        detail = first_line
                else:
                    if is_active:
                        status = "working"
                        detail = "Thinking…"
                    else:
                        status = "completed"
                        detail = "Task completed"
            elif role == "tool":
                detail = f"Tool result: {tool_name or 'completed'}"
                status = "working" if is_active else "completed"
            elif role == "user":
                if is_active:
                    detail = "Thinking…"
                    status = "working"
                else:
                    detail = "Ready for prompt"
                    status = "idle"
        # If detail is still not set or was generic, look for the last assistant response
        if not detail or detail == "Ready for prompt":
            cur.execute(
                "SELECT substr(content, 1, 2048) FROM messages WHERE session_id = ? AND role = 'assistant' AND content IS NOT NULL ORDER BY id DESC LIMIT 1;",
                (session_id,)
            )
            ast_row = cur.fetchone()
            if ast_row and ast_row[0]:
                first_line = extract_first_line(ast_row[0])
                if first_line:
                    detail = first_line

        conn.close()

        effective_prompt = latest_user_prompt or redact_secrets(best["title"] or "")
        return (
            effective_prompt,
            clean_model_name(best["model"] or "ox-alpha-free"),
            best["provider"] or "",
            best["profile"] or "",
            detail or f"Profile: {best['profile'] or 'Default'}",
            status,
            has_question,
        )
    except Exception:
        return None, None, None, None, None, None, False

def extract_hermes_latest_session() -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], bool]:
    """Backward-compatible wrapper for extract_hermes_session_info."""
    return extract_hermes_session_info()


def shorten_path(path: str) -> str:
    """Abbreviate path with ~ for user home directory."""
    if not path:
        return ""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~/" + path[len(home) + 1 :]
    return path


# --- Orca-managed terminal agents -------------------------------------------
#
# Orca manages terminals inside its own GUI (worktrees, agent tabs). The
# `orca terminal list --json` CLI is a single bounded subprocess call that
# reports every live managed terminal with its worktree path, branch, tab
# title, orphan/connected state, last-output timestamp and a raw preview
# snippet. Agents running in those terminals never appear as standalone
# Hyprland windows, so they are invisible to scan_standalone_agents() and must
# be collected here.

ORCA_CLI = os.environ.get("ORCA_CLI_PATH", "orca")
_ORCA_CACHE: Dict[str, Any] = {"ts": 0.0, "terminals": []}
_ORCA_CACHE_TTL = 5.0  # seconds; keeps repeated fetch cycles cheap

# Map an Orca terminal to a known agent type from its cleaned title. Titles
# like "Hermes", "OMP" or "Pi" are set by Orca's own tab-title detection.
_ORCA_AGENT_ALIASES = {
    "hermes": "hermes",
    "omp": "omp",
    "pi": "omp",
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "gemini": "gemini",
    "agy": "agy",
    "cursor": "cursor",
    "cline": "cline",
    "grok": "grok",
}

# Bare shells / system programs that mean "no agent in this terminal".
_ORCA_BARE_SHELLS = {
    "zsh", "bash", "sh", "fish", "nushell", "nu", "dash", "ksh",
    "alberto@omarchy", "omarchy", "shell", "pacman", "vim", "nvim",
}

# While an agent works inside a tab, Orca renames the tab to a dynamic title
# like "<status/spinner glyph> <task summary> · <model>[ · <extra>]", e.g.
# "✓ Prevent new instance on she… · ox-alpha-free · ~". Titles truncate (the
# "…" above), so match the STRUCTURE — arbitrary task text, then
# " · "-separated segments starting with a model token — never the exact
# string. The leading glyph is OPTIONAL because clean_title() already strips
# Braille spinners before this pattern runs (check marks survive it).
_ORCA_DYNAMIC_TITLE_RE = re.compile(
    r"^[\u2800-\u28FF✓✗✳✻✷✸✹*·•]*\s*"
    r"(?P<task>.+?)"
    r"\s+·\s+(?P<model>\S+)"
    r"(?:\s+·\s+.*)?$"
)

# Model tokens from dynamic titles mapped to the CLI that typically owns them
# (Hermes titles carry "ox-alpha-free"; Claude carries "opus"/"sonnet"/...).
# Most specific prefixes first.
_ORCA_MODEL_AGENT_HINTS = (
    ("ox", "hermes"),
    ("opus", "claude"),
    ("sonnet", "claude"),
    ("haiku", "claude"),
    ("codex", "codex"),
    ("gpt", "codex"),
    ("o3", "codex"),
    ("o4", "codex"),
    ("gemini", "gemini"),
    ("grok", "grok"),
)

# Unambiguous agent-TUI chrome strings (beyond the CLI's own name) that can
# corroborate a dynamic title's model hint in the terminal preview.
_ORCA_PREVIEW_CHROME_MARKERS = {
    "hermes": ("hermes --tui", "voice off", "voice on"),
    "omp": ("π",),
    "claude": ("? for shortcuts", "⏵⏵", "✻"),
    "gemini": ("gemini cli",),
    "codex": (),
    "opencode": (),
    "agy": (),
    "grok": (),
}

# Agent names looked up literally (as words) in the preview when the title's
# model token yields no hint. Ordered most-specific first.
_ORCA_PREVIEW_AGENT_NAMES = ("hermes", "opencode", "gemini", "claude", "codex", "omp", "agy", "pi", "grok")


def _contains_word(haystack: str, word: str) -> bool:
    """True when `word` occurs in `haystack` not glued to other letters/digits."""
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", haystack) is not None


# Full-screen TUIs (agent CLIs included) paint their frames/rules with
# box-drawing characters; interactive shell prompts use powerline glyphs
# (private-use codepoints) instead. Enough box-drawing in a preview means a
# full-screen app lives in the tab, not a bare shell.
_ORCA_TUI_FRAME_RE = re.compile(r"[│┃┆┄─═╭╮╰╯┌┐└┘]")


def _agent_from_model_token(token: str) -> Optional[str]:
    """Map a model token from a dynamic tab title to a likely agent type."""
    t = token.strip("…").strip().lower()
    if not t:
        return None
    for prefix, agent in _ORCA_MODEL_AGENT_HINTS:
        if t == prefix or t.startswith(prefix):
            return agent
    return None


def _preview_confirms_agent(preview_lower: str, agent_type: str, model_token: str) -> bool:
    """Decide whether a terminal's preview evidences the given agent TUI."""
    # The TUI echoing its own model (raw, or separators rendered as spaces).
    if model_token:
        variants = {model_token, model_token.replace("-", " ").replace("_", " ")}
        if any(v in preview_lower for v in variants):
            return True
    # Agent-specific TUI chrome (status bars, hints, key legends).
    if any(m and m.lower() in preview_lower for m in _ORCA_PREVIEW_CHROME_MARKERS.get(agent_type, ())):
        return True
    # The CLI's own name spelled out in the visible output.
    own_name = next((n for n, a in _ORCA_AGENT_ALIASES.items() if a == agent_type and n != "pi"), "")
    if own_name and _contains_word(preview_lower, own_name):
        return True
    # Generic full-screen TUI frame: box-drawing structure a shell never draws.
    if len(_ORCA_TUI_FRAME_RE.findall(preview_lower)) >= 3:
        return True
    return False


def classify_orca_terminal(term: Dict[str, Any]) -> Optional[str]:
    """Map one Orca terminal to a known agent type.

    Two recognition paths:
      1. Static titles set by Orca's own tab detection ("Hermes", "OMP", ...)
         via a first-word alias lookup.
      2. Dynamic titles ("<glyph> <task> · <model>") renamed by the running
         agent, confirmed against the terminal's preview so bare shells and
         unrelated output never classify as agents.

    Returns None for bare shells and anything unrecognized so only real
    agents surface in the roster.
    """
    cleaned = clean_title(str(term.get("title") or "")).strip()
    lowered = cleaned.lower()
    if not lowered:
        return None
    if lowered in _ORCA_BARE_SHELLS or lowered.startswith("alberto@"):
        return None
    first_word = lowered.split()[0]
    static_type = _ORCA_AGENT_ALIASES.get(first_word)
    if static_type:
        return static_type

    # Dynamic agent titles: require the structural title pattern AND positive
    # evidence in the preview that an agent TUI really lives here.
    match = _ORCA_DYNAMIC_TITLE_RE.match(lowered)
    if not match:
        return None

    preview_lower = clean_ansi(str(term.get("preview") or "")).lower()
    if not preview_lower:
        return None

    hinted = _agent_from_model_token(match.group("model"))

    # 1) The title names a model: require the preview to corroborate that the
    #    matching agent TUI really lives here.
    if hinted:
        model_token = match.group("model").strip("…").strip().lower()
        if _preview_confirms_agent(preview_lower, hinted, model_token):
            return hinted

    # 2) No usable hint (or it failed corroboration): trust a literal agent
    #    name in the preview over the title's guesswork.
    for name in _ORCA_PREVIEW_AGENT_NAMES:
        if _contains_word(preview_lower, name):
            return _ORCA_AGENT_ALIASES.get(name)

    return None


_ORCA_MAX_BYTES = 256 * 1024  # 256 KiB hard cap on raw CLI output
_ORCA_MAX_TERMINALS = 64      # maximum terminals to retain from one fetch
_ORCA_MAX_FIELD = 4096        # per-field string cap (preview, title, etc.)


def _cap_fields(term: Dict[str, Any]) -> Dict[str, Any]:
    """Truncate large string fields to prevent memory amplification."""
    for key in ("preview", "title", "worktreePath", "branch", "handle"):
        val = term.get(key)
        if isinstance(val, str) and len(val) > _ORCA_MAX_FIELD:
            term[key] = val[:_ORCA_MAX_FIELD]
    return term


def query_orca_terminals(timeout: float = 4.0) -> List[Dict[str, Any]]:
    """Return the live terminals known to the Orca runtime, cached briefly.

    Single bounded subprocess invocation with structural output capping:
    the raw stdout is read through a 256 KiB ceiling so a runaway Orca
    endpoint cannot exhaust memory before JSON parsing.  Terminal count
    and per-field sizes are also capped.

    On success the result is cached for _ORCA_CACHE_TTL seconds.  On
    failure (CLI missing, timeout, non-JSON) we do NOT poison the cache
    with an empty list — instead we return the most recent good result
    (or [] on the very first call) and leave the cache untouched, so a
    transient Orca CLI hang can't blank out the roster for the whole TTL.
    """
    now = time.monotonic()
    if now - _ORCA_CACHE["ts"] < _ORCA_CACHE_TTL:
        return _ORCA_CACHE["terminals"]

    terminals: List[Dict[str, Any]] = []
    try:
        proc = subprocess.Popen(
            [ORCA_CLI, "terminal", "list", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # Read at most _ORCA_MAX_BYTES from stdout so the producer cannot
        # exhaust memory regardless of how much it writes.
        chunks: List[str] = []
        total = 0
        while total < _ORCA_MAX_BYTES:
            remaining = _ORCA_MAX_BYTES - total
            chunk = proc.stdout.read(min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise subprocess.TimeoutExpired(proc.args, timeout)
        stdout = "".join(chunks)

        if proc.returncode == 0 and stdout.strip():
            payload = json.loads(stdout)
            if isinstance(payload, dict) and payload.get("ok"):
                result = payload.get("result") or {}
                raw = result.get("terminals")
                if isinstance(raw, list):
                    terminals = [
                        _cap_fields(t)
                        for t in raw
                        if isinstance(t, dict)
                    ][:_ORCA_MAX_TERMINALS]
                # Only cache a successful parse; otherwise fall through to the
                # last-known-good branch below.
                _ORCA_CACHE["ts"] = now
                _ORCA_CACHE["terminals"] = terminals
                return terminals
    except FileNotFoundError:
        print("agent_ctl: orca CLI not found; skipping Orca terminal scan", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("agent_ctl: orca terminal list timed out; serving last-known-good", file=sys.stderr)
    except Exception as e:
        # Non-JSON output, transient runtime errors, etc. -> treat as no data.
        print(f"agent_ctl: orca terminal list failed ({e}); serving last-known-good", file=sys.stderr)

    # Failure path: return the last good cache (may be [] on first call) without
    # refreshing the timestamp, so the next cycle retries the CLI promptly.
    return _ORCA_CACHE["terminals"]


def scan_orca_agents(claimed_sessions: Set[str]) -> List[Dict[str, Any]]:
    """Discover AI agents running inside Orca-managed terminals.

    Excludes bare shells, disconnected and orphaned terminals; dedupes against
    other sources via claimed_sessions when session enrichment applies.
    """
    orca_agents = []
    for term in query_orca_terminals():
        try:
            # Skip orphaned (runtime lost the pty) and dead terminals; keep a
            # little slack for connected=false flaps on freshly spawned tabs.
            if term.get("orphaned"):
                continue
            if not term.get("connected", True) and not term.get("lastOutputAt"):
                continue

            agent_type = classify_orca_terminal(term)
            if not agent_type:
                continue

            cwd = str(term.get("worktreePath") or "")
            repo_name = os.path.basename(cwd.rstrip("/")) if cwd else ""
            clean_cwd = shorten_path(cwd)

            branch_raw = str(term.get("branch") or "")
            branch_name = branch_raw.split("/")[-1] if branch_raw else ""

            handle = str(term.get("handle") or "")
            pane_id = f"orca:term:{handle}"

            # Enrich from the same transcript sessions other sources use, so
            # prompt/model/status match the rest of the roster. Claimed
            # sessions are shared to avoid double-counting one transcript.
            session_path = find_latest_session_for_cwd(agent_type, cwd, claimed_sessions)
            user_goal = None
            detail_text = None
            model_name = None
            status_override = None
            has_question = False

            if session_path:
                claimed_sessions.add(session_path)
                if agent_type == "omp" and os.path.exists(session_path):
                    user_goal, detail_text, model_name, status_override, has_question = extract_omp_task_from_session(session_path)
                elif agent_type == "grok":
                    user_goal, detail_text, model_name, status_override, has_question = extract_grok_task_from_session(session_path)
                elif agent_type == "hermes":
                    _, model_name, _, _, detail_text, status_override, has_question = extract_hermes_session_info(source_preference="cli")
            elif agent_type == "omp":
                model_name = get_omp_default_model()

            # Fall back to Orca's own preview snippet for the detail line:
            # ANSI-stripped, secret-redacted, first meaningful line only.
            preview_line = extract_first_line(clean_ansi(str(term.get("preview") or "")))
            detail_display = detail_text or user_goal or preview_line or clean_cwd

            title_candidates = [
                user_goal,
                clean_title(str(term.get("title") or "")),
                f"{agent_type.capitalize()} in {repo_name}" if repo_name else "",
            ]
            effective_title = next((t for t in title_candidates if t), f"{agent_type.upper()} in Orca")

            # Status: live session inference wins, then recency of output.
            # A terminal that produced output within the last 30s while we
            # could not prove otherwise is treated as working.
            if status_override:
                effective_status = status_override
                if has_question and status_override != "waiting":
                    effective_status = "waiting"
            else:
                last_out_ms = term.get("lastOutputAt")
                recent = bool(last_out_ms) and (time.time() * 1000 - float(last_out_ms)) < 30000
                effective_status = "working" if recent else "idle"

            workspace_label = f"Orca · {repo_name}" if repo_name else "Orca"

            orca_agents.append({
                "pane_id": pane_id,
                "pid": None,
                "origin": "orca",
                "origin_label": "Orca Terminal",
                "agent": agent_type,
                "agent_display": agent_type.upper() if agent_type in ("omp", "pi") else agent_type.capitalize(),
                "status": effective_status,
                "title": effective_title,
                "detail": detail_display,
                "cwd": clean_cwd,
                "repo": repo_name,
                "workspace": workspace_label,
                "tab": str(term.get("title") or "").strip() or "Orca terminal",
                "pane_label": branch_name or "Orca worktree",
                "focused": False,
                "model": clean_model_name(model_name) if model_name else "",
                "session_path": session_path or "",
                "has_question": has_question,
            })
        except Exception:
            continue
    return orca_agents


def scan_standalone_agents(herdr_server_pids: List[int], seen_cwds: Set[str], claimed_sessions: Set[str]) -> List[Dict[str, Any]]:
    """Discover AI agents running in normal terminal windows outside of Herdr."""
    standalone = []
    hermes_desktop_found = False

    for p in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(p))
            info = get_process_info(pid)
            if not info or not info["cmd"]:
                continue
            cmd = info["cmd"]

            ancestors = get_process_ancestors(pid)
            ancestor_pids = [a["pid"] for a in ancestors]

            is_in_herdr = any(hp in ancestor_pids for hp in herdr_server_pids)
            is_broker_child = any(
                "daemon_broker" in a["cmd"] or "__omp_worker" in a["cmd"] or "runner-" in a["cmd"]
                for a in ancestors
            )

            # Skip anything running inside Herdr, spawned as an internal background worker, or system usage script
            if is_in_herdr or is_broker_child or is_claude_helper_process(cmd, info.get("argv")) or is_grok_helper_process(cmd, info.get("argv")) or "omarchy-agent-usage" in cmd or "__omp_worker" in cmd or "gateway run" in cmd or "serve --host" in cmd or "zygote" in cmd or "agent_ctl.py" in cmd:
                continue

            # Check if this is a Hermes Desktop GUI process
            if "/Hermes" in cmd and "--type=" not in cmd and not hermes_desktop_found:
                hermes_desktop_found = True
                if "hermes_desktop" in seen_cwds or is_in_herdr:
                    continue

                hermes_title, hermes_model, hermes_provider, hermes_profile, hermes_detail, hermes_status, hermes_has_q = extract_hermes_session_info(source_preference="desktop")
                standalone.append({
                    "pane_id": f"desktop:hermes:{pid}",
                    "pid": pid,
                    "origin": "desktop",
                    "origin_label": "Hermes Desktop",
                    "agent": "hermes",
                    "agent_display": "Hermes Desktop",
                    "status": hermes_status or "idle",
                    "title": hermes_title or "Hermes Desktop Workspace",
                    "detail": hermes_detail or f"Profile: {hermes_profile or 'Default'}",
                    "cwd": "~/.hermes",
                    "repo": "Hermes Desktop",
                    "workspace": "Desktop App",
                    "tab": f"Hermes GUI (PID {pid})",
                    "pane_label": "Electron Window",
                    "focused": False,
                    "model": hermes_model or "ox-alpha-free",
                    "session_path": "",
                    "has_question": hermes_has_q,
                })
                continue

            tokens = cmd.split()
            first = os.path.basename(tokens[0])

            agent_type = None
            if first in ("omp", "pi"):
                agent_type = "omp"
            elif first == "agy":
                # Google Antigravity CLI - no JSONL transcripts; generic enrichment
                agent_type = "agy"
            elif first == "grok":
                agent_type = "grok"
            elif first in ("claude", "codex", "opencode", "cline", "cursor"):
                agent_type = first
            elif first in ("python", "python3") and is_hermes_cli_process(cmd):
                agent_type = "hermes"

            if agent_type:
                cwd = info.get("cwd", "")
                repo_name = os.path.basename(cwd.rstrip("/")) if cwd else ""
                clean_cwd = shorten_path(cwd)

                term_name = None
                for anc in ancestors:
                    anc_cmd = anc["cmd"].lower()
                    for t in KNOWN_TERMINALS:
                        if t in anc_cmd:
                            term_name = t
                            break
                    if term_name:
                        break

                matched_client = match_hypr_client_for_terminal(ancestor_pids, cwd, agent_type)
                if not matched_client:
                    # Process is headless, orphaned, or terminal was closed -> skip it
                    continue

                window_addr = matched_client.get("address", "")
                if not window_addr:
                    continue

                if not term_name:
                    client_class = matched_client.get("class") or matched_client.get("initialClass") or ""
                    if client_class:
                        term_name = client_class.split(".")[-1].lower()
                term_name = term_name or "terminal"

                ws_id = str(matched_client.get("workspace", {}).get("name", matched_client.get("workspace", {}).get("id", "1")))
                workspace_name = f"Desktop {ws_id}"
                tab_name = f"{term_name.capitalize()} (Desktop {ws_id})"
                pane_id = f"terminal:addr:{window_addr}"
                # A hook-reported Claude status is authoritative: it comes from
                # the session itself, so it wins over transcript inference and
                # over the process-state guess further down.
                claude_hook_status = read_claude_hook_status(pid) if agent_type == "claude" else None
                session_path = None if claude_hook_status else find_session_for_process(agent_type, pid, cwd, claimed_sessions, info.get("argv"))
                if agent_type == "grok" and session_path:
                    # Only a real `.cwd` marker (long-path slug+hash groups) may
                    # override the breadcrumb: falling back to the decoded group
                    # name here would print a slug+hash as if it were a path.
                    session_cwd = grok_group_cwd_marker(os.path.dirname(session_path))
                    if session_cwd:
                        cwd = session_cwd
                        repo_name = os.path.basename(cwd.rstrip("/")) if cwd else ""
                        clean_cwd = shorten_path(cwd)
                user_goal = None
                detail_text = None
                model_name = None
                status_override = None
                has_question = False

                if claude_hook_status:
                    status_override = claude_hook_status["status"]
                    detail_text = claude_hook_status["detail"] or "Ready for prompt"
                    has_question = status_override == "waiting"
                elif session_path:
                    claimed_sessions.add(session_path)
                    if agent_type == "omp":
                        if os.path.exists(session_path):
                            user_goal, detail_text, model_name, status_override, has_question = extract_omp_task_from_session(session_path)
                        else:
                            model_name = get_omp_default_model()
                    elif agent_type == "grok":
                        user_goal, detail_text, model_name, status_override, has_question = extract_grok_task_from_session(session_path)
                    elif agent_type == "hermes":
                        p_st = get_process_start_time(pid)
                        user_goal, model_name, _, _, detail_text, status_override, has_question = extract_hermes_session_info(source_preference="cli", min_start_time=p_st)
                else:
                    if agent_type == "omp":
                        model_name = get_omp_default_model()
                    detail_text = "Ready for prompt"
                    status_override = "idle"

                if not model_name and agent_type == "omp":
                    model_name = get_omp_default_model()
                if user_goal:
                    effective_title = user_goal
                elif repo_name and repo_name not in ("tmp", "~"):
                    effective_title = f"{agent_type.upper()} session in {repo_name}"
                else:
                    effective_title = f"{agent_type.upper()} session ({repo_name or '~'})"

                detail_display = detail_text or clean_cwd
                effective_status = status_override or ("working" if info.get("state") in ("R", "D") else "idle")

                standalone.append({
                    "pane_id": pane_id,
                    "pid": pid,
                    "origin": "terminal",
                    "origin_label": f"Terminal ({term_name.capitalize()})",
                    "agent": agent_type,
                    "agent_display": agent_type.upper() if agent_type in ("omp", "pi") else agent_type.capitalize(),
                    "status": effective_status,
                    "title": effective_title,
                    "detail": detail_display,
                    "cwd": clean_cwd,
                    "repo": repo_name,
                    "workspace": workspace_name,
                    "tab": tab_name,
                    "pane_label": f"PID {pid}",
                    "focused": False,
                    "model": model_name or "",
                    "session_path": session_path or "",
                    "has_question": has_question,
                })
        except Exception:
            continue
    return standalone


def fetch_all_agents() -> Dict[str, Any]:
    """Fetch all agent data, combining Herdr snapshot, session enrichment, and standalone processes."""
    # Grok session reads are budgeted per fetch cycle, so a large or hostile
    # $GROK_HOME cannot turn one tick into unbounded work.
    _GROK_BUDGET.reset()
    herdr_pids = get_herdr_server_pids()

    agents_list = []
    working_count = 0
    completed_count = 0
    idle_count = 0
    waiting_count = 0
    active_agent_types = set()
    top_working_task = ""
    top_completed_task = ""
    seen_cwds: Set[str] = set()
    claimed_sessions: Set[str] = set()
    herdr_connected = False
    all_workspaces: List[str] = []

    def consume_herdr_snapshot(snap: Dict[str, Any], sess_name: str, sess_sock: str) -> None:
        """Parse one Herdr session snapshot and merge its agents into the results.

        Each Herdr session is an independent server with its own socket and
        ID namespace, so pane/tab/workspace maps are per-snapshot and every
        agent records the session it came from.
        """
        nonlocal working_count, completed_count, idle_count, waiting_count, top_working_task, top_completed_task

        workspaces_map = {}
        tabs_map = {}
        panes_map = {}

        for ws in snap.get("workspaces", []):
            workspaces_map[ws.get("workspace_id")] = ws.get("label") or f"Workspace {ws.get('number', 1)}"
        for ws_label in workspaces_map.values():
            display = ws_label if sess_name == "default" else f"{sess_name}: {ws_label}"
            if display not in all_workspaces:
                all_workspaces.append(display)

        for tab in snap.get("tabs", []):
            tabs_map[tab.get("tab_id")] = tab.get("label") or f"Tab {tab.get('number', 1)}"

        for pane in snap.get("panes", []):
            panes_map[pane.get("pane_id")] = pane

        raw_agents = snap.get("agents", [])
        for a in raw_agents:
            pane_id = a.get("pane_id")
            pane_info = panes_map.get(pane_id, {})

            agent_type = (a.get("agent") or "agent").lower()
            raw_status = (a.get("agent_status") or "idle").lower()

            cwd = a.get("foreground_cwd") or a.get("cwd") or pane_info.get("foreground_cwd") or pane_info.get("cwd") or ""
            repo_name = os.path.basename(cwd.rstrip("/")) if cwd else ""
            clean_cwd = shorten_path(cwd)

            title_raw = a.get("terminal_title_stripped") or a.get("terminal_title") or pane_info.get("terminal_title_stripped") or ""
            cleaned_title = clean_title(title_raw)

            tab_id = a.get("tab_id") or pane_info.get("tab_id")
            workspace_id = a.get("workspace_id") or pane_info.get("workspace_id")

            tab_name = tabs_map.get(tab_id, "")
            workspace_name = workspaces_map.get(workspace_id, "")
            if workspace_name and sess_name != "default":
                workspace_name = f"{sess_name}: {workspace_name}"
            pane_label = pane_info.get("label") or a.get("label") or ""

            is_hermes_desktop = False
            agent_launch = pane_info.get("agent_launch") or a.get("agent_launch") or {}
            if agent_type == "hermes" and (agent_launch.get("args") == ["desktop"] or "desktop" in tab_name.lower()):
                is_hermes_desktop = True
                seen_cwds.add("hermes_desktop")

            agent_session = a.get("agent_session", {})
            session_path = agent_session.get("value") if isinstance(agent_session, dict) else None

            if not session_path or not os.path.exists(session_path):
                session_path = find_latest_session_for_cwd(agent_type, cwd, claimed_sessions)

            if session_path:
                claimed_sessions.add(session_path)

            user_goal = None
            detail_text = None
            model_name = None
            status_override = None
            has_question = False

            if agent_type == "omp" and session_path:
                user_goal, detail_text, model_name, status_override, has_question = extract_omp_task_from_session(session_path)
            elif agent_type == "grok" and session_path:
                user_goal, detail_text, model_name, status_override, has_question = extract_grok_task_from_session(session_path)
            elif agent_type == "hermes":
                hermes_p_start = None
                for hp in glob.glob("/proc/[0-9]*"):
                    try:
                        hpid = int(os.path.basename(hp))
                        hinfo = get_process_info(hpid)
                        if hinfo and "hermes" in hinfo["cmd"].lower() and "gateway" not in hinfo["cmd"] and "zygote" not in hinfo["cmd"]:
                            if is_hermes_desktop and ("/Hermes" in hinfo["cmd"] or "hermes desktop" in hinfo["cmd"]):
                                hermes_p_start = get_process_start_time(hpid)
                                break
                            elif not is_hermes_desktop and is_hermes_cli_process(hinfo["cmd"]):
                                hermes_p_start = get_process_start_time(hpid)
                                break
                    except Exception:
                        pass
                if is_hermes_desktop:
                    user_goal, model_name, _, _, detail_text, status_override, has_question = extract_hermes_session_info(source_preference="desktop", min_start_time=hermes_p_start)
                else:
                    user_goal, model_name, _, _, detail_text, status_override, has_question = extract_hermes_session_info(source_preference="cli", min_start_time=hermes_p_start)
            is_generic_title = cleaned_title in (repo_name, "~", "tmp", "/tmp", "") or cleaned_title.startswith("/tmp") or cleaned_title.startswith("alberto@")

            if user_goal:
                effective_title = user_goal
            elif cleaned_title and not is_generic_title:
                effective_title = cleaned_title
            elif pane_label:
                effective_title = pane_label
            elif repo_name and repo_name not in ("tmp", "~"):
                effective_title = f"Working in {repo_name}"
            else:
                effective_title = f"{agent_type.upper()} session"
            # Determine effective status. Herdr's live agent_status is authoritative;
            # session-file inference only fills gaps and must NEVER downgrade a
            # live "working" report (a finished previous turn in the transcript tail
            # does not mean the current turn is done).
            if status_override == "waiting":
                status = "waiting"
            elif raw_status in ("waiting", "prompt", "input"):
                status = "waiting"
            elif raw_status in ("working", "busy", "running", "thinking", "generating"):
                status = "working"
            elif status_override == "working":
                status = "working"
            elif raw_status in ("completed", "done", "finished") or "✓" in title_raw:
                status = "completed"
            elif raw_status in ("error", "failed"):
                status = "error"
            else:
                # Herdr has no strong opinion (idle/empty): fall back to the
                # session tail, which distinguishes a finished task from a fresh prompt.
                status = status_override or "idle"

            origin = "herdr_desktop" if is_hermes_desktop else "herdr"
            origin_label = "Herdr (Desktop)" if is_hermes_desktop else "Herdr"
            display_name = "Hermes Desktop" if is_hermes_desktop else (agent_type.upper() if agent_type in ("omp", "pi") else agent_type.capitalize())

            if status == "working":
                working_count += 1
                active_agent_types.add(agent_type)
                if not top_working_task:
                    top_working_task = f"{display_name}: {effective_title}"
            elif status == "waiting":
                waiting_count += 1
                active_agent_types.add(agent_type)
            elif status == "completed":
                completed_count += 1
                if not top_completed_task:
                    top_completed_task = f"{display_name}: ✓ {effective_title}"
            else:
                idle_count += 1
            detail_display = detail_text or (user_goal if user_goal and user_goal != effective_title else "") or clean_cwd

            agents_list.append(
                {
                    "pane_id": f"herdr:{sess_name}|{pane_id}",
                    "raw_pane_id": pane_id,
                    "herdr_session": sess_name,
                    "origin": origin,
                    "origin_label": origin_label,
                    "agent": agent_type,
                    "agent_display": display_name,
                    "status": status,
                    "title": effective_title,
                    "detail": detail_display,
                    "cwd": clean_cwd,
                    "repo": repo_name,
                    "workspace": workspace_name,
                    "tab": tab_name,
                    "pane_label": pane_label,
                    "focused": bool(a.get("focused")),
                    "model": model_name or (get_omp_default_model() if agent_type == "omp" else ""),
                    "session_path": session_path or "",
                    "has_question": has_question,
                }
            )

    for sess_name, sess_sock in herdr_session_sockets():
        resp = query_herdr_socket("session.snapshot", sock_path=sess_sock)
        if resp and "result" in resp and "snapshot" in resp["result"]:
            herdr_connected = True
            consume_herdr_snapshot(resp["result"]["snapshot"], sess_name, sess_sock)

    # Orca-managed terminals run before the standalone process scan so they
    # can claim transcript sessions first; the standalone scan skips anything
    # whose session was claimed here, which dedupes agents visible to both.
    orca_agents = scan_orca_agents(claimed_sessions)

    standalone_agents = scan_standalone_agents(herdr_pids, seen_cwds, claimed_sessions)
    for sa in standalone_agents:
        if sa["status"] == "working":
            working_count += 1
            active_agent_types.add(sa["agent"])
            if not top_working_task:
                top_working_task = f"{sa['agent_display']}: {sa['title']}"
        elif sa["status"] == "waiting":
            waiting_count += 1
            active_agent_types.add(sa["agent"])
        elif sa["status"] == "completed":
            completed_count += 1
            if not top_completed_task:
                top_completed_task = f"{sa['agent_display']}: ✓ {sa['title']}"
        else:
            idle_count += 1
        agents_list.append(sa)

    for oa in orca_agents:
        if oa["status"] == "working":
            working_count += 1
            active_agent_types.add(oa["agent"])
            if not top_working_task:
                top_working_task = f"{oa['agent_display']}: {oa['title']}"
        elif oa["status"] == "waiting":
            waiting_count += 1
            active_agent_types.add(oa["agent"])
        elif oa["status"] == "completed":
            completed_count += 1
            if not top_completed_task:
                top_completed_task = f"{oa['agent_display']}: ✓ {oa['title']}"
        else:
            idle_count += 1
        agents_list.append(oa)

    def agent_sort_key(item: Dict[str, Any]) -> Tuple[int, int, str]:
        status_order = {"working": 0, "waiting": 1, "completed": 2, "error": 3, "idle": 4}
        return (status_order.get(item["status"], 5), 0 if item.get("focused") else 1, item["agent"])

    agents_list.sort(key=agent_sort_key)

    total = len(agents_list)
    if waiting_count > 0:
        waiting_agent = next((a for a in agents_list if a["status"] == "waiting"), None)
        if waiting_agent and waiting_agent.get("detail"):
            headline = f"{waiting_agent['agent_display']}: {waiting_agent['detail']}"
        else:
            headline = f"{waiting_count} agent{'s' if waiting_count > 1 else ''} awaiting input"
    elif working_count > 0:
        headline = top_working_task or f"{working_count} agent{'s' if working_count > 1 else ''} busy"
    elif completed_count > 0:
        headline = top_completed_task or f"{completed_count} agent{'s' if completed_count > 1 else ''} completed"
    elif total > 0:
        headline = f"{total} agent{'s' if total > 1 else ''} idle"
    else:
        headline = "No active agents"
    all_workspaces_dedup: List[str] = []
    for ws in all_workspaces:
        if ws and ws not in all_workspaces_dedup:
            all_workspaces_dedup.append(ws)
    for sa in standalone_agents + orca_agents:
        ws = sa.get("workspace")
        if ws and ws not in all_workspaces_dedup:
            all_workspaces_dedup.append(ws)

    return {
        "ok": True,
        "connected": herdr_connected,
        "herdr_sessions": [{"name": n, "socket": s} for n, s in herdr_session_sockets()],
        "orca_connected": bool(query_orca_terminals()),
        "summary": {
            "total": total,
            "working": working_count,
            "completed": completed_count,
            "idle": idle_count,
            "waiting": waiting_count,
            "active_agents": sorted(list(active_agent_types)),
            "headline": headline,
        },
        "agents": agents_list,
        "workspaces": all_workspaces_dedup,
    }


def find_herdr_window_for_session(clients: List[Dict[str, Any]], session_name: str) -> Optional[Dict[str, Any]]:
    """Find the Hyprland window whose terminal client is attached to a Herdr session.

    Terminal clients run `herdr` (default session) or `herdr --session <name>`
    as a direct child process, so pair a window to a session by reading that
    child process's command line.
    """
    for c in clients:
        client_pid = c.get("pid")
        if not client_pid:
            continue
        for p in glob.glob("/proc/[0-9]*"):
            try:
                child = get_process_info(int(os.path.basename(p)))
                if not child or child["ppid"] != client_pid:
                    continue
                cmd = child["cmd"]
                if f"herdr --session {session_name}" in cmd:
                    return c
                if session_name == "default" and (cmd == "herdr" or cmd.endswith("/herdr")):
                    return c
            except Exception:
                continue
    return None


def focus_pane(target_id: str) -> Dict[str, Any]:
    """Focus a specific pane in Herdr, Hermes Desktop window, or standalone terminal and switch desktops."""
    if not target_id:
        return {"ok": False, "error": "No target_id provided"}

    clients = get_hypr_clients()

    # 1. Hermes Desktop GUI window
    if target_id.startswith("desktop:hermes:") or target_id.startswith("desktop:") or target_id == "w1:p4":
        hermes_win = next(
            (c for c in clients if c.get("class") == "Hermes" or c.get("initialClass") == "Hermes"),
            None,
        )
        if hermes_win:
            focus_hypr_window(hermes_win)
            return {
                "ok": True,
                "target": target_id,
                "focused_window": "Hermes Desktop",
                "workspace": hermes_win.get("workspace", {}).get("id"),
            }
        if target_id.startswith("desktop:"):
            return {"ok": False, "error": "Hermes GUI window not found"}
    # 2. Standalone terminal window
    if target_id.startswith("terminal:addr:"):
        addr = target_id.split("terminal:addr:")[1]
        if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", addr):
            return {"ok": False, "error": "Invalid window address format"}
        target_win = next((c for c in clients if c.get("address") == addr), None)
        if target_win:
            focus_hypr_window(target_win)
            return {
                "ok": True,
                "target": target_id,
                "focused_window": target_win.get("title", ""),
                "workspace": target_win.get("workspace", {}).get("id"),
            }

    if target_id.startswith("terminal:pid:"):
        pid_str = target_id.split("terminal:pid:")[1]
        try:
            target_pid = int(pid_str)
            ancestors = get_process_ancestors(target_pid)
            anc_pids = [target_pid] + [a["pid"] for a in ancestors]
            standalone_win = match_hypr_client_for_terminal(anc_pids, "", "")
            if standalone_win:
                focus_hypr_window(standalone_win)
                return {
                    "ok": True,
                    "target": target_id,
                    "focused_window": standalone_win.get("title", ""),
                    "workspace": standalone_win.get("workspace", {}).get("id"),
                }
        except Exception:
            pass
        return {"ok": False, "error": "Standalone terminal window not found"}
    # 3. Herdr pane (session-qualified targets look like herdr:<session>|<pane_id>)
    sess_name = "default"
    pane_target = target_id
    if target_id.startswith("herdr:"):
        parts = target_id.split("|", 1)
        sess_name = parts[0][len("herdr:"):]
        pane_target = parts[1] if len(parts) > 1 else target_id
    sock_path = herdr_socket_for_session(sess_name)
    if os.path.exists(sock_path):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(sock_path)
            req = {"jsonrpc": "2.0", "id": "focus:exec", "method": "pane.focus", "params": {"pane_id": pane_target}}
            s.sendall((json.dumps(req) + "\n").encode())
            try:
                s.recv(4096)
            except Exception:
                pass
            s.close()
        except Exception:
            pass
    herdr_win = find_herdr_window_for_session(clients, sess_name)
    if not herdr_win:
        herdr_win = next(
            (c for c in clients if "MAIN" in c.get("title", "") or "herdr" in c.get("title", "").lower()),
            None,
        )
    if not herdr_win:
        herdr_win = next(
            (c for c in clients if c.get("class") in ("com.mitchellh.ghostty", "org.omarchy.terminal", "foot", "alacritty", "kitty")),
            None,
        )

    if herdr_win:
        focus_hypr_window(herdr_win)
        return {
            "ok": True,
            "pane_id": pane_target,
            "herdr_session": sess_name,
            "focused_window": herdr_win.get("title", ""),
            "workspace": herdr_win.get("workspace", {}).get("id"),
        }

    return {"ok": True, "pane_id": pane_target, "herdr_session": sess_name}

def kill_target(target_id: str) -> Dict[str, Any]:
    """Gracefully terminate an agent process or close a Herdr pane."""
    if not target_id:
        return {"ok": False, "error": "No target_id provided"}

    env = get_hypr_env()

    # 1. Standalone terminal process
    if target_id.startswith("terminal:pid:"):
        pid_str = target_id.replace("terminal:pid:", "")
        try:
            pid = int(pid_str)
            if not is_valid_agent_process(pid):
                return {"ok": False, "error": f"PID {pid} is not a recognized agent process"}
            ancestors = get_process_ancestors(pid)
            clients = get_hypr_clients()
            ancestor_pids = [pid] + [a["pid"] for a in ancestors]
            matched_win = match_hypr_client_for_terminal(ancestor_pids, "", "")
            if matched_win and matched_win.get("address"):
                win_addr = matched_win["address"]
                if re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", str(win_addr)):
                    try:
                        lua_close = f'hl.dsp.window.close({{ window = "address:{win_addr}" }})'
                        subprocess.run(
                            ["hyprctl", "dispatch", lua_close],
                            env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=0.5,
                        )
                    except Exception:
                        pass
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            return {"ok": True, "killed_pid": pid}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 2. Hermes Desktop window
    if target_id.startswith("desktop:hermes:"):
        pid_str = target_id.replace("desktop:hermes:", "")
        try:
            pid = int(pid_str)
            if not is_valid_agent_process(pid):
                return {"ok": False, "error": f"PID {pid} is not a recognized Hermes process"}
            try:
                subprocess.run(
                    ["hyprctl", "dispatch", 'hl.dsp.window.close({ window = "class:Hermes" })'],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=0.5,
                )
            except Exception:
                pass
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            return {"ok": True, "killed_pid": pid}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 3. Herdr pane (session-qualified targets look like herdr:<session>|<pane_id>)
    if target_id.startswith("herdr:"):
        parts = target_id.split("|", 1)
        sess_name = parts[0][len("herdr:"):]
        pane_target = parts[1] if len(parts) > 1 else target_id
        res = query_herdr_socket("pane.close", {"pane_id": pane_target}, sock_path=herdr_socket_for_session(sess_name))
        return {"ok": True, "pane_id": pane_target, "herdr_session": sess_name, "socket_res": res}
    res = query_herdr_socket("pane.close", {"pane_id": target_id})
    return {"ok": True, "pane_id": target_id, "socket_res": res}


def launch_agent(agent_name: Optional[str] = None) -> Dict[str, Any]:
    """Launch agent in Omarchy or open agent selector."""
    cmd = ["omarchy-agent", "--pick"] if not agent_name else ["omarchy-agent", agent_name]
    try:
        subprocess.Popen(cmd)
        return {"ok": True, "command": cmd}
    except Exception as e:
        return {"ok": False, "error": str(e)}


STATUS_MAX_AGENTS = 256      # agent cards kept in one status payload
STATUS_MAX_WORKSPACES = 128  # workspace labels kept in one status payload
STATUS_MAX_BYTES = 262144    # hard ceiling on the serialized status payload


_STATUS_TEXT_MAX = 400       # per-string clip applied to agent-supplied fields
_STATUS_NUMBER_BOUND = 10 ** 12


def _clip_text(value: Any, limit: int = _STATUS_TEXT_MAX) -> str:
    """Coerce a display string to a whitespace-flattened, clipped form."""
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return " ".join(text.split())[:limit]


def _bounded_number(value: Any) -> Any:
    """Bound numeric payload fields so a pathological int cannot break json.dumps.

    Python refuses to serialize ints past ~4300 digits; this is not reachable from
    session files today (every file-sourced value is str()-coerced or a bounded
    count) but the serializer is the last line of defence, so it is clamped here.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    try:
        if isinstance(value, float):
            return value if value == value and abs(value) != float("inf") else 0.0
        return max(-_STATUS_NUMBER_BOUND, min(_STATUS_NUMBER_BOUND, int(value)))
    except Exception:
        return 0


def _dumps(payload: Dict[str, Any]) -> str:
    """json.dumps that never raises: unencodable values fall back to their str()."""
    try:
        return json.dumps(payload, indent=2)
    except Exception:
        try:
            return json.dumps(payload, indent=2, default=str)
        except Exception:
            return ""


def dump_status_json(data: Dict[str, Any]) -> str:
    """Serialize the status payload inside a hard, structural size bound.

    The panel reads this through a StdioCollector with no cap of its own and then
    slices whatever arrives to 262144 characters before JSON.parse, so the reply
    has to be valid JSON *within* that bound — truncating mid-JSON would just
    freeze the widget on stale data. Every agent-supplied string is clipped first
    (a single hostile session file must not be able to inflate the reply), then the
    agent list is capped, and only if the result still exceeds the ceiling is the
    payload reduced to numeric counts.
    """
    payload = dict(data)
    agents = payload.get("agents")
    if isinstance(agents, list):
        clipped: List[Any] = []
        for agent in agents[:STATUS_MAX_AGENTS]:
            if isinstance(agent, dict):
                clipped.append({k: (_clip_text(v) if isinstance(v, str) else _bounded_number(v)) for k, v in agent.items()})
        payload["agents"] = clipped
    summary = payload.get("summary")
    if isinstance(summary, dict):
        payload["summary"] = {
            k: (_clip_text(v, 300) if isinstance(v, str) else _bounded_number(v))
            for k, v in summary.items()
        }
    workspaces = payload.get("workspaces")
    if isinstance(workspaces, list):
        payload["workspaces"] = [_clip_text(w, 200) for w in workspaces[:STATUS_MAX_WORKSPACES]]
    text = _dumps(payload)
    if text and len(text) <= STATUS_MAX_BYTES:
        return text

    # Still oversized (or unserializable): keep the counts, drop every free-text
    # field, and bound the numbers so this reply cannot grow with any input.
    counts: Dict[str, Any] = {}
    for key, value in (summary if isinstance(summary, dict) else {}).items():
        if isinstance(value, (bool, int, float)):
            counts[key] = _bounded_number(value)
        elif key == "active_agents" and isinstance(value, list):
            counts[key] = [_clip_text(item, 40) for item in value[:32]]
    trimmed = {
        "ok": bool(payload.get("ok", True)),
        "connected": bool(payload.get("connected", False)),
        "herdr_sessions": [],
        "orca_connected": bool(payload.get("orca_connected", False)),
        "summary": counts,
        "agents": [],
        "workspaces": [],
        "truncated": True,
    }
    text = _dumps(trimmed)
    if not text or len(text) > STATUS_MAX_BYTES:
        text = _dumps({
            "ok": True,
            "connected": False,
            "summary": {"total": _bounded_number(counts.get("total") or 0)},
            "agents": [],
            "workspaces": [],
            "truncated": True,
        })
    return text


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("fetch", "status", "--json", "-j"):
        data = fetch_all_agents()
        print(dump_status_json(data))
        return

    cmd = sys.argv[1]
    if cmd in ("focus", "--focus", "-f"):
        if len(sys.argv) < 3:
            print(json.dumps({"ok": False, "error": "Missing pane_id/target_id"}))
            sys.exit(1)
        target_id = sys.argv[2]
        result = focus_pane(target_id)
        print(json.dumps(result))
        return

    if cmd in ("kill", "--kill", "stop", "--stop", "-k"):
        if len(sys.argv) < 3:
            print(json.dumps({"ok": False, "error": "Missing pane_id/target_id"}))
            sys.exit(1)
        target_id = sys.argv[2]
        result = kill_target(target_id)
        print(json.dumps(result))
        return

    if cmd in ("launch", "--launch", "-l"):
        agent_name = sys.argv[2] if len(sys.argv) > 2 else None
        result = launch_agent(agent_name)
        print(json.dumps(result))
        return

    print(json.dumps({"ok": False, "error": f"Unknown command {cmd}"}))
    sys.exit(1)


if __name__ == "__main__":
    main()
