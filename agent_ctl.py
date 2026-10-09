#!/usr/bin/env python3
"""
agent_ctl.py - Backend collector and orchestrator controller for Omarchy Agent Orchestrator.
Discovers and manages AI agents across:
1. Herdr workspaces & panes (Herdr daemon socket + process tree correlation)
2. Standard terminal windows (Foot, Alacritty, Kitty, Ghostty)
3. Hermes Desktop GUI instances (Electron app)
4. Grok Build TUI sessions ($GROK_HOME/active_sessions.json + events.jsonl)
5. Remote Hermes peers (gateways registered with `hermes peer add`, read-only)
"""

import glob
import http.client
import ipaddress
import json
import math
import os
import pwd
import re
import signal
import selectors
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor

import socket
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote, unquote, urljoin, urlsplit

HERDR_SOCK_PATH = os.path.expanduser(os.environ.get("HERDR_SOCKET_PATH", "~/.config/herdr/herdr.sock"))
OMP_SESSIONS_DIR = os.path.expanduser("~/.omp/agent/sessions")
HERMES_STATE_DB = os.path.expanduser("~/.hermes/state.db")
HERMES_CONNECTIONS_PATH = os.path.expanduser("~/.config/Hermes/connections.json")
HERDR_MACHINE_TIMEOUT = 0.75
HERDR_REMOTE_TIMEOUT = 2.5
HERDR_REMOTE_MAX_BYTES = 262144

# --- Confirmed-file reads (task 2) ---------------------------------------------
# Two profiles, one implementation. `strict` is for the credential file and only
# the credential file; `bounded` is for non-secret config. Applying the strict
# recipe to a 0644 config would silently empty the roster, and applying the lax
# one to a .env would be a marketplace credential-path block.
HERMES_READ_MAX_BYTES = {
    "env": 65536,             # strict: ~/.hermes/.env
    "config": 262144,         # bounded: ~/.hermes/config.yaml
    "registry": 262144,       # bounded: ~/.config/Hermes/connections.json
}
HERMES_CONFINE_MODE = 0o022
HERMES_CREDENTIAL_MODE = 0o077
# Roots are named, never derived from an arbitrary env value: ~/.hermes,
# ~/.config/Hermes, and $HERMES_HOME only when it resolves strictly inside a
# validated $HOME (an env-derived root may only widen from inside).
HERMES_HOME_ENV = "HERMES_HOME"


class ConfinedFileError(Exception):
    """A file was refused by the confined reader.

    Callers degrade softly: one stderr line, exit 0, valid JSON on stdout. The
    widget parses stdout, so a warning there would read as a parse error.
    """


class _SwappedFile(Exception):
    """The file was replaced between validation and open; one retry is allowed."""


def _trusted_dir(path: str, ancestor_uids: Sequence[int], trust_mode: int = HERMES_CONFINE_MODE) -> bool:
    """True when `path` is an existing directory, ours-or-root owned, not group/other writable."""
    try:
        info = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    if info.st_uid not in ancestor_uids:
        return False
    return not (stat.S_IMODE(info.st_mode) & trust_mode)


def _chain_trusted(path: str, ancestor_uids: Sequence[int], trust_root: str) -> bool:
    """Walk every ancestor of `path` up to the trust anchor; all must be trusted."""
    current = os.path.dirname(path)
    while True:
        if not _trusted_dir(current, ancestor_uids):
            return False
        if current == trust_root or current == "/":
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return True
        current = parent


def validated_home(uid: Optional[int] = None, ancestor_uids: Optional[Sequence[int]] = None) -> Optional[str]:
    """Resolve $HOME as a TRUSTED input, never via expanduser().

    expanduser("~") returns "/" for an empty HOME, which makes an "inside the
    home" test true for every absolute path; a relative HOME resolves against
    the cwd. Both are treated as no home at all. `/` itself is no home.

    `ancestor_uids` is the injectable trust anchor for the directory walk. The
    default is (us, root). A bwrap dev session maps only our own uid, so root
    reads as 65534 and the default correctly refuses; the suite injects the uid
    that sandbox actually shows rather than the guard being weakened.
    """
    own = os.getuid() if uid is None else uid
    trusted_uids: Sequence[int] = (own, 0) if ancestor_uids is None else tuple(ancestor_uids)
    raw = os.environ.get("HOME") or ""
    if not raw or not os.path.isabs(raw):
        try:
            raw = pwd.getpwuid(own).pw_dir or ""
        except Exception:
            return None
    if not raw or not os.path.isabs(raw):
        return None
    real = os.path.realpath(raw)
    if real == "/":
        return None
    if not _chain_trusted(real, trusted_uids, "/"):
        return None
    return real


def hermes_confine_roots(
    uid: Optional[int] = None,
    home: Optional[str] = None,
    ancestor_uids: Optional[Sequence[int]] = None,
) -> List[str]:
    """The roots a Hermes file may live in. An env-derived root only widens from inside."""
    own = os.getuid() if uid is None else uid
    base = validated_home(own, ancestor_uids) if home is None else home
    if not base:
        return []
    roots = [os.path.join(base, ".hermes"), os.path.join(base, ".config", "Hermes")]
    declared = os.environ.get(HERMES_HOME_ENV) or ""
    if declared and os.path.isabs(declared):
        real = os.path.realpath(declared)
        # STRICTLY inside the validated home: with HOME=/ the inside-home test is
        # true for everything, which is why validated_home() refuses "/".
        if real != base and real.startswith(base.rstrip(os.sep) + os.sep):
            roots.append(real)
    deduped: List[str] = []
    for root in roots:
        if root and root not in deduped:
            deduped.append(root)
    return deduped


def _within(real: str, root: str) -> bool:
    root = os.path.realpath(root).rstrip(os.sep)
    return real == root or real.startswith(root + os.sep)


def confined_path(
    path: str,
    roots: Sequence[str],
    *,
    trust_root: str = "/",
    ancestor_uids: Sequence[int] = (0,),
    uid: Optional[int] = None,
    home: Optional[str] = None,
) -> str:
    """Validate a path into a trusted root and return its realpath.

    The checks, in order: non-empty, no NUL byte, absolute (after expanding `~`
    against a validated home ourselves), no `..` component, NOT a symlink at the
    normalised path, realpath strictly inside a named root, and every ancestor of
    that realpath trusted. `normpath` runs BEFORE `islink` because islink("link/")
    is False for a symlink to a regular file, so a trailing slash walks past it.
    """
    own = os.getuid() if uid is None else uid
    raw = str(path or "")
    if not raw:
        raise ConfinedFileError("empty path")
    if "\x00" in raw:
        raise ConfinedFileError("NUL byte in path")
    if raw == "~" or raw.startswith("~/"):
        # Forward BOTH the injected home and the trust anchor: expanding `~`
        # against the real $HOME while the roots point at a fixture would refuse
        # every tilde path (and, on a read-only $HOME, would be unfixable).
        base = validated_home(own, ancestor_uids) if home is None else home
        if not base:
            raise ConfinedFileError("no validated home")
        raw = base if raw == "~" else os.path.join(base, raw[2:])
    if not os.path.isabs(raw):
        raise ConfinedFileError("path must be absolute")
    if ".." in raw.split(os.sep):
        raise ConfinedFileError("parent traversal in path")
    candidate = os.path.normpath(raw)
    if os.path.islink(candidate):
        raise ConfinedFileError("symlink at the exact path")
    real = os.path.realpath(candidate)
    allowed = [r for r in roots if r]
    if not any(_within(real, root) for root in allowed):
        raise ConfinedFileError("outside every trusted root")
    if not _chain_trusted(real, ancestor_uids, trust_root):
        raise ConfinedFileError("unsafe directory chain")
    return real


def read_confined_text(
    kind: str,
    path: str,
    *,
    roots: Optional[Sequence[str]] = None,
    trust_root: str = "/",
    ancestor_uids: Optional[Sequence[int]] = None,
    uid: Optional[int] = None,
    home: Optional[str] = None,
) -> Optional[str]:
    """Read one confined file's text, or None after refusing softly.

    `kind` selects the profile: "env" is the strict credential recipe, every
    other kind is the bounded non-secret one. The handle is trusted, not the
    path: os.open with O_NOFOLLOW|O_NONBLOCK, then fstat, then samestat against
    the validated path, then /proc/self/fd containment. O_NONBLOCK is load-bearing
    — a planted FIFO passes every path check and would wedge the open forever.
    An unresolvable fd reference FAILS CLOSED rather than skipping the only half
    that sees a swapped directory or mount point.
    """
    own = os.getuid() if uid is None else uid
    trusted_uids: Sequence[int] = (own, 0) if ancestor_uids is None else tuple(ancestor_uids)
    strict = kind == "env"
    max_bytes = HERMES_READ_MAX_BYTES.get(kind, HERMES_READ_MAX_BYTES["config"])
    effective_roots = list(roots) if roots is not None else hermes_confine_roots(own, home, trusted_uids)

    failure: Optional[BaseException] = None
    for _attempt in range(2):  # one bounded retry: an atomic os.replace can land mid-read
        try:
            real = confined_path(
                path, effective_roots, trust_root=trust_root,
                ancestor_uids=trusted_uids, uid=own, home=home,
            )
            fd = os.open(real, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None  # absent is normal (no .env means no peers), and silent
        except (ConfinedFileError, OSError) as exc:
            failure = exc
            break  # a refusal is deterministic; a retry would change nothing

        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ConfinedFileError("not a regular file")
            if info.st_uid != own:
                raise ConfinedFileError("not owned by this user")
            if info.st_size > max_bytes:
                raise ConfinedFileError("larger than the read ceiling")
            if strict:
                if info.st_nlink != 1:
                    raise ConfinedFileError("hard-linked credential file")
                if stat.S_IMODE(info.st_mode) & HERMES_CREDENTIAL_MODE:
                    raise ConfinedFileError("credential file readable beyond its owner")
            if not os.path.samestat(info, os.stat(real)):
                raise _SwappedFile()
            descriptor = os.path.realpath(f"/proc/self/fd/{fd}")
            if not any(_within(descriptor, root) for root in effective_roots if root):
                raise ConfinedFileError("descriptor is not contained in a trusted root")
            chunks: List[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            body = b"".join(chunks)
            if len(body) > max_bytes:
                raise ConfinedFileError("larger than the read ceiling")
            return body.decode("utf-8", errors="replace")
        except _SwappedFile:
            failure = ConfinedFileError("file changed during the read")
            continue
        except (ConfinedFileError, OSError) as exc:
            failure = exc
            break
        finally:
            os.close(fd)

    if failure is not None:
        print(
            f"agent_ctl: refused {kind} file: {type(failure).__name__}: {failure}",
            file=sys.stderr,
        )
    return None


def parse_herdr_machine_list(value: Any) -> List[Dict[str, str]]:
    """Keep enabled saved Herdr machines, without auth or private config fields."""
    if not isinstance(value, list):
        return []
    machines: List[Dict[str, str]] = []
    for row in value[:64]:
        if not isinstance(row, dict) or row.get("enabled") is False:
            continue
        machine_id = str(row.get("id") or "").strip()
        target = str(row.get("target") or "").strip()
        if not machine_id or not target:
            continue
        machines.append({
            "id": machine_id,
            "label": str(row.get("label") or target).strip()[:120],
            "target": target,
            "session": str(row.get("session") or "default").strip() or "default",
        })
    return machines


def herdr_machine_list() -> List[Dict[str, str]]:
    """Read saved machines through Herdr's existing local CLI."""
    try:
        proc = subprocess.run(
            ["herdr", "machine", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=HERDR_MACHINE_TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            return []
        return parse_herdr_machine_list(json.loads(proc.stdout))
    except Exception:
        return []


def remote_herdr_command(machine: Dict[str, str], operation: str = "api snapshot") -> List[str]:
    """Build safe SSH argv for supported read-only Herdr operations."""
    target = str(machine.get("target") or "").strip()
    session = str(machine.get("session") or "default").strip()
    if not session or not re.fullmatch(r"[A-Za-z0-9_.-]+", session):
        raise ValueError("invalid saved Herdr session")
    ssh_target: List[str]
    if target.startswith("ssh://"):
        parsed = urlsplit(target)
        if parsed.scheme != "ssh" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("invalid saved Herdr SSH URI")
        ssh_target = []
        if parsed.username:
            ssh_target.append(f"{parsed.username}@{parsed.hostname}")
        else:
            ssh_target.append(parsed.hostname)
        if parsed.port:
            ssh_target = ["-p", str(parsed.port)] + ssh_target
    elif target and re.fullmatch(r"[A-Za-z0-9_.:@%+,-]+", target):
        ssh_target = [target]
    else:
        raise ValueError("invalid saved Herdr SSH target")
    if operation not in {"api snapshot"} and not re.fullmatch(r"agent read [A-Za-z0-9_.:-]+ --source detection", operation):
        raise ValueError("invalid saved Herdr operation")
    remote = f'exec "$(command -v herdr || for p in "$HOME/.local/bin/herdr" "$HOME/bin/herdr" /usr/local/bin/herdr /usr/bin/herdr; do [ -x "$p" ] && printf %s "$p" && break; done)" --session {session} {operation}'
    ssh_options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=2"]
    if ssh_target[:1] == ["-p"]:
        ssh_options += ssh_target[:2]
        ssh_target = ssh_target[2:]
    return ["ssh", *ssh_options, "--", *ssh_target, remote]


def remote_herdr_target_id(machine_id: str, session: str, pane_id: str) -> str:
    """Build a collision-proof remote pane ID for UI routing."""
    return f"herdr-remote:{machine_id}:{session}|{pane_id}"


def run_bounded_remote_command(command: List[str]) -> Optional[str]:
    """Run read-only SSH command with timeout and pre-decode output ceiling."""
    proc = None
    selector = selectors.DefaultSelector()
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stdout, selectors.EVENT_READ)
        chunks: List[bytes] = []
        total = 0
        deadline = time.monotonic() + HERDR_REMOTE_TIMEOUT
        while time.monotonic() < deadline:
            events = selector.select(max(0.0, deadline - time.monotonic()))
            if not events:
                break
            try:
                chunk = os.read(proc.stdout.fileno(), min(32768, HERDR_REMOTE_MAX_BYTES + 1 - total))
            except BlockingIOError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > HERDR_REMOTE_MAX_BYTES:
                return None
        if proc.poll() is None:
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.returncode != 0 or not chunks:
            return None
        return b"".join(chunks).decode("utf-8", errors="replace")
    except Exception:
        return None
    finally:
        selector.close()
        if proc is not None and proc.poll() is None:
            proc.kill()


def query_remote_herdr_snapshot(machine: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Query one saved machine through Herdr's read-only API command."""
    try:
        stdout = run_bounded_remote_command(remote_herdr_command(machine))
        if stdout is None:
            return None
        response = json.loads(stdout)
        return response if isinstance(response, dict) else None
    except Exception:
        return None


def query_herdr_pane_detection(pane_id: str, sock_path: str) -> Optional[str]:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", str(pane_id or "")):
        return None
    response = query_herdr_socket(
        "pane.read",
        {"pane_id": pane_id, "source": "detection", "format": "text"},
        sock_path=sock_path,
    )
    read = (response or {}).get("result", {}).get("read", {})
    return read.get("text") if isinstance(read, dict) else None


def query_remote_herdr_agent_read(machine: Dict[str, str], pane_id: str) -> Optional[str]:
    """Read remote agent detection text without touching remote files."""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", str(pane_id or "")):
        return None
    try:
        command = remote_herdr_command(machine)
        command[-1] = command[-1].replace(" api snapshot", f" agent read {pane_id} --source detection")
        return run_bounded_remote_command(command)
    except Exception:
        return None


def hermes_screen_status(text: Optional[str]) -> Optional[str]:
    """Infer Hermes activity from screen text; stale agent labels are insufficient."""
    value = str(text or "").lower()
    if not value:
        return None
    if any(marker in value for marker in ("enter to confirm", "enter confirm", "hermes needs your", "dangerous command")):
        return "waiting"
    if any(marker in value for marker in ("⏳", "ctrl+c cancel", "ctrl+c to interrupt", "thinking…", "running tool")):
        return "working"
    if "✓" in value or "ready for prompt" in value:
        return "idle"
    return None


# --- Remote Hermes peers (issue #21) -------------------------------------------
# A peer is another Hermes gateway the user registered with
# `hermes peer add <name> --url http://host:8377 --key <API_SERVER_KEY>`. Hermes
# owns that credential; the plugin only reads the one key it needs through the
# strict confined reader and never touches a Desktop OAuth token. The peer
# answers /api/sessions + /api/sessions/<id>/messages on its api_server platform.
PEER_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")
PEER_SESSION_ID_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,128}\Z")
PEER_URL_MAX = 300
PEER_MAX_PEERS_PARSED = 8      # entries kept from the config block
PEER_MAX_PEERS_PROBED = 4      # peers actually queried per tick, in config order
PEER_MAX_PEER_CARDS = 6        # cards per peer in the payload
PEER_MAX_MESSAGE_READS = 3     # per-peer message reads (the HTTP budget)
PEER_MAX_BYTES = 262144        # pre-decode ceiling on one response
PEER_REQ_BUDGET = 1.0          # per-request socket timeout
PEER_PEER_BUDGET = 3.0         # TOTAL per-peer budget for one tick
PEER_ACTIVE_WINDOW = 300.0     # "active" = ended_at is None and last_active within this
PEER_STALE_WINDOW = 14400.0    # 4 h: older than this without a lease is history, not a session
PEER_SESSIONS_QUERY = 20       # limit= on GET /api/sessions
PEER_BOT_CHAT_TITLE = "Bot Chat"
# Explicit trust anchor, NOT create_default_context(): that honours SSL_CERT_FILE /
# SSL_CERT_DIR / SSLKEYLOGFILE from the session env, which would make the trust
# store a property of the environment instead of the code. For the same reason
# there is NO env-honouring fallback below — a missing bundle means no peer TLS.
PEER_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
PEER_HOST_RE = re.compile(r"\A[a-z0-9]([a-z0-9.-]*[a-z0-9])?\Z")


def _peer_tls_context() -> Optional["ssl.SSLContext"]:
    """TLS context pinned to the system bundle, or None — never a loose default.

    Returning None makes peer_http_get refuse that peer, which is the correct
    outcome on a host without the bundle. The tempting fallback
    (`create_default_context()`, no cafile) was a real finding: it honours
    SSL_CERT_FILE / SSL_CERT_DIR from the session env, so a host missing the
    bundle would have had the peer's trust store decided by the environment.
    Verified: with a generated CA in SSL_CERT_FILE the loose context loaded that
    CA and nothing else, while the pinned one loaded 121 system CAs.
    """
    try:
        context = ssl.create_default_context(cafile=PEER_CA_BUNDLE)
    except Exception:
        return None
    try:
        context.keylog_filename = None
    except Exception:
        pass
    return context


def parse_bot_peers(text: Optional[str]) -> List[Dict[str, str]]:
    """Read the `bot_peers:` block of config.yaml, keeping only well-shaped rows.

    Deliberately NOT a YAML parser and NOT `hermes config get`: a vendor's
    documented layout is not byte-level truth, so anything unexpected is
    ignored rather than guessed (degrade to "no card"), and spawning a
    PATH-resolved Python inside an auto-running widget is the attack surface a
    marketplace reviewer counts. Only `^  <name>:$` + `^    url: <value>$` is
    accepted; `note:`, comments, quoted scalars and deeper nesting are skipped.
    """
    if not text:
        return []
    peers: List[Dict[str, str]] = []
    inside = False
    block_indent = 0
    current: Optional[int] = None  # index of the peer that may accept a url: line
    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if not inside:
            # The top-level `bot_peers:` key only. A nested `bot_peers:` under
            # another section is not ours.
            if stripped == "bot_peers:" and indent == 0:
                inside = True
            continue
        if indent <= 0:
            break  # next top-level key: the block ended
        if indent == 2 and stripped.endswith(":"):
            # `current` is the index of the peer whose url we may accept. A name
            # that fails the charset check, or arrives past the cap, sets it to
            # None so its own url: line is DISCARDED instead of being written
            # into the previously accepted peer (which would send that peer's key
            # to the rejected entry's host).
            block_indent = 2
            name = stripped[:-1].strip()
            if PEER_NAME_RE.match(name) and len(peers) < PEER_MAX_PEERS_PARSED:
                peers.append({"name": name, "url": ""})
                current = len(peers) - 1
            else:
                current = None
            continue
        # The url: must be a DIRECT child of the accepted name (indent == name
        # indent + 2). `indent > block_indent` was too loose: a nested url:, or
        # one under any later sibling, overwrote the accepted peer — which is how
        # a peer's key reaches a host the user never typed (found by the round-2
        # security reviewer: spark's url became http://evil.lan:8377).
        if current is not None and indent == block_indent + 2 and stripped.startswith("url:"):
            value = stripped[len("url:"):].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]  # a quoted scalar is still a plain scalar here
            if not peers[current]["url"]:  # first url wins; never overwrite
                peers[current]["url"] = value
            current = None
    return [p for p in peers if p["name"] and p["url"]][:PEER_MAX_PEERS_PARSED]


def scrub_peer_text(value: Any, secrets: Optional[Sequence[str]] = None, limit: int = 400) -> str:
    """The ONE sanitiser for every peer-supplied string that reaches the bar.

    A peer is an untrusted remote: `redact_secrets` only catches key-SHAPED text,
    but a peer API key is an opaque string, so a peer (or anything on-path) that
    echoes the key it just received into a title or tool name would otherwise get
    it rendered in an always-on bar. Hence: ANSI out, control bytes out, known
    secret patterns out, each loaded key out, then a hard length cap.

    `clean_model_name` and `_clip_text` are NOT on this path — the first strips
    nothing but a provider prefix, the second flattens whitespace and keeps
    NUL/ESC. Callers that skip this function are the bug this exists to prevent.
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    text = clean_ansi(text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", " ", text)
    text = redact_secrets(text)
    for secret in secrets or ():
        # Longest first, and only substantial values: a 1-char "key" would
        # otherwise shred every string it appears in.
        if isinstance(secret, str) and len(secret) >= 8:
            text = text.replace(secret, "[REDACTED]")
    return " ".join(text.split())[:limit]


def peer_base_url(value: str) -> Optional[str]:
    """Normalise a peer URL, refusing userinfo, a query, a fragment or a path.

    `hermes peer add --url http://user:pass@host:8377` would otherwise send Basic
    auth beside the Bearer header and leak that password into the payload, a
    stderr line and a URLError message. A non-root path prefix is refused too:
    every request target is built with urljoin on this base, so an embedded path
    would silently change what is asked for.
    """
    raw = str(value or "").strip()
    if not raw or len(raw) > PEER_URL_MAX:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    if parts.path not in ("", "/"):
        return None
    try:
        port = parts.port  # raises ValueError on a non-numeric port
    except ValueError:
        return None
    host = parts.hostname.lower().rstrip(".")
    if not host or not PEER_HOST_RE.match(host):
        return None
    authority = f"[{host}]" if ":" in host else host
    if port:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def peer_host_is_loopback(base_url: str) -> bool:
    """True for every spelling of this machine, so local cards are not duplicated.

    `urlsplit().hostname` already strips IPv6 brackets, and Python's ipaddress
    REJECTS the alternate literal forms (`127.1`, `0x7f000001`,
    `::ffff:127.0.0.1` mapped) that a resolver still routes to loopback — so each
    of those is handled explicitly instead of being assumed.
    """
    try:
        raw = (urlsplit(base_url or "").netloc or "").lower()
    except ValueError:
        return False
    if "@" in raw:  # userinfo is refused upstream; never let it steer this check
        return False
    if raw.startswith("["):
        host, _, port = raw.partition("]")
        host = host[1:]
        port = port.lstrip(":")
    else:
        host, _, port = raw.partition(":")
    host = host.rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True

    def _loopback(literal: str) -> Optional[bool]:
        try:
            return ipaddress.ip_address(literal).is_loopback
        except ValueError:
            return None

    verdict = _loopback(host)
    if verdict is not None:
        return verdict
    # Alternate literal spellings a resolver still routes to loopback but
    # ipaddress rejects: `127.1`, `127.0.53`, `0x7f000001`, `2130706433`.
    # socket.inet_aton IS the C library's parser for these, so delegate rather
    # than re-deriving its padding and radix rules (getting them wrong would
    # either miss a real loopback or skip a real remote host).
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return False
    return ipaddress.ip_address(packed).is_loopback


def peer_session_status(
    message: Optional[Dict[str, Any]],
    active: bool,
) -> Tuple[str, str, bool]:
    """(status, detail, has_question) for one peer session.

    Mirrors the local extract_hermes_session_info() semantics on the data the
    peer's api_server actually exposes. api_server has no turn-lease table, so
    `active` is the dashboard's 300 s heuristic and a turn that stalls with no
    new message reads as working — stated in the README rather than hidden.
    """
    detail = ""
    status = "idle"
    has_question = False
    if not isinstance(message, dict):
        # Not polled (over the read budget or past the deadline). The local
        # scanner has the same "unknown" status, and claiming "working" for a
        # session we never read would be a guess shown as fact.
        return "unknown", "Not polled (message budget)", False

    role = message.get("role")
    content = message.get("content") if isinstance(message.get("content"), str) else ""
    tool_name = message.get("tool_name") if isinstance(message.get("tool_name"), str) else ""
    tool_calls = message.get("tool_calls")

    if role == "assistant" and tool_calls:
        status = "working"
        detail = "Running tool"
    elif role == "tool":
        detail = f"Tool result: {tool_name or 'completed'}" if tool_name else "Tool result"
        status = "working" if active else "completed"
    elif role == "assistant" and content:
        first_line = extract_first_line(content, max_len=140)
        has_question = "?" in (first_line[-40:] if first_line else "")
        if active:
            status, detail = "working", (first_line or "Generating response…")
        else:
            status = "waiting" if has_question else "completed"
            detail = first_line
    elif role == "user":
        if active:
            status, detail = "working", "Thinking…"
        else:
            status, detail = "idle", "Ready for prompt"
    else:
        status = "working" if active else "completed"
        detail = "Thinking…" if active else "Task completed"
    return status, detail, has_question


def peer_card_title(row: Dict[str, Any]) -> str:
    """Title preference: an explicit title, else the cleaned preview."""
    for key in ("title", "preview"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def build_peer_agents(
    peers: List[Dict[str, str]],
    now: float,
    *,
    keys: Optional[Dict[str, str]] = None,
    fetch: Any = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Query each peer and build read-only cards. Returns (cards, peer_status).

    Budget per peer (the bar polls every 2-3 s and this runs every tick, in a
    FRESH process — there is no cross-tick cache to lean on):
      1 x GET /api/sessions?limit=20
      1 x GET /api/sessions?title=Bot Chat&include_hidden=1
      <=3 message reads, most-recently-active first
    all under ONE total per-peer deadline (PEER_PEER_BUDGET), peers in parallel.
    A peer that fails yields no card and a stated reason, never an exception.
    """
    get = fetch or peer_http_get
    cards: List[Dict[str, Any]] = []
    status_rows: List[Dict[str, Any]] = []
    for entry in peers:
        name = str(entry.get("name") or "")
        base = entry.get("base") or peer_base_url(entry.get("url") or "")
        if not name or not base or not PEER_NAME_RE.match(name):
            status_rows.append({"name": name[:64], "url": "", "reachable": False, "sessions": 0,
                                "error": "refused: unusable peer entry"})
            continue
        if peer_host_is_loopback(base):
            # This machine: local discovery already covers it, and showing both
            # would render every session twice.
            status_rows.append({"name": name[:64], "url": base[:PEER_URL_MAX], "reachable": True,
                                "sessions": 0, "error": "skipped: loopback (already discovered locally)"})
            continue
        key = (keys or {}).get(name) or load_peer_secret(name)
        if not key:
            status_rows.append({"name": name[:64], "url": base[:PEER_URL_MAX], "reachable": False,
                                "sessions": 0, "error": f"no API key ({peer_secret_env_name(name)} unset)"})
            continue
        try:
            peer_cards, reachable, reason = _build_one_peer(name, base, key, now, get)
        except Exception as exc:  # a peer must never take the roster down
            peer_cards, reachable, reason = [], False, f"error: {type(exc).__name__}"
        cards.extend(peer_cards)
        status_rows.append({
            "name": name[:64], "url": base[:PEER_URL_MAX], "reachable": reachable,
            "sessions": len(peer_cards), "error": reason,
        })
    return cards[:PEER_MAX_PEERS_PROBED * PEER_MAX_PEER_CARDS], status_rows


def _build_one_peer(
    name: str,
    base: str,
    key: str,
    now: float,
    get: Any,
) -> Tuple[List[Dict[str, Any]], bool, str]:
    """One peer's cards, under a single total deadline."""
    deadline = time.monotonic() + PEER_PEER_BUDGET
    first = get(base, f"/api/sessions?limit={PEER_SESSIONS_QUERY}", key, deadline=deadline)
    if first is None:
        # None covers refused, timed out, 3xx, non-2xx, a blown budget and
        # unparseable JSON — none of which are "a healthy peer with no sessions".
        return [], False, "unreachable or no usable answer"
    rows = parse_peer_sessions(first)
    if not rows:
        # Distinguish "no sessions" from "unreachable" for the diagnostics row:
        # re-ask for the Bot Chat, which a hidden filter would otherwise hide.
        bot = parse_peer_sessions(get(
            base,
            f"/api/sessions?title={quote(PEER_BOT_CHAT_TITLE)}&include_hidden=1&limit=4",
            key, deadline=deadline,
        ))
        return _cards_from_rows(name, base, key, bot, now, get, deadline), True, "no sessions in the last window"

    combined = list(rows)
    seen_ids = {peer_session_id(r) for r in rows}
    if time.monotonic() < deadline:
        bot = parse_peer_sessions(get(
            base,
            f"/api/sessions?title={quote(PEER_BOT_CHAT_TITLE)}&include_hidden=1&limit=4",
            key, deadline=deadline,
        ))
        for row in bot:
            sid = peer_session_id(row)
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                combined.append(row)
    return _cards_from_rows(name, base, key, combined, now, get, deadline), True, ""


def _cards_from_rows(
    name: str,
    base: str,
    key: str,
    rows: List[Dict[str, Any]],
    now: float,
    get: Any,
    deadline: float,
) -> List[Dict[str, Any]]:
    """Turn session rows into cards, spending at most PEER_MAX_MESSAGE_READS."""
    candidates: List[Tuple[str, Dict[str, Any]]] = []
    for row in rows:
        sid = peer_session_id(row)
        if not sid:
            continue
        try:
            last_active = float(row.get("last_active") or 0)
        except (TypeError, ValueError):
            last_active = 0.0
        # A row with neither messages nor a title is noise, and history older
        # than the stale window is not a live session (same rule as the local path).
        if not row.get("message_count") and not peer_card_title(row):
            continue
        # `not (x > 0)` is False for NaN, which kills the NaN case in one clause;
        # isfinite() drops +inf, which would otherwise compare as never-stale and
        # produce a card that never ages out.
        if not (last_active > 0) or not math.isfinite(last_active):
            continue
        if (now - last_active) > PEER_STALE_WINDOW:
            continue
        candidates.append((sid, row))

    # Most recently active first, so the message-read budget buys the rows that
    # can actually be "working".
    candidates.sort(key=lambda item: float(item[1].get("last_active") or 0), reverse=True)
    candidates = candidates[:PEER_MAX_PEER_CARDS]

    secrets = (key,)
    cards: List[Dict[str, Any]] = []
    reads = 0
    for sid, row in candidates:
        message = None
        if reads < PEER_MAX_MESSAGE_READS and time.monotonic() < deadline:
            reads += 1
            encoded = quote(sid, safe="")
            payload = get(base, f"/api/sessions/{encoded}/messages?limit=1&order=latest", key, deadline=deadline)
            message = parse_peer_message(payload)
        active = peer_session_is_active(row, now)
        status, detail, has_question = peer_session_status(message, active)
        title = scrub_peer_text(peer_card_title(row), secrets) or "Hermes session"
        cards.append({
            "pane_id": peer_target_id(name, sid),
            "origin": "hermes_peer",
            "origin_label": f"Hermes · {name}",
            # Bounded: a legal peer name is up to 64 chars, and the pill has to
            # elide rather than push the status pill and terminate button off the
            # card (measured: 137px of overflow at the user's 1.18 scale factor).
            "origin_badge": (f"PEER · {name}")[:48],
            "agent": "hermes",
            "agent_display": "Hermes",
            "status": status,
            "title": title,
            "detail": scrub_peer_text(detail, secrets, limit=200),
            "cwd": "",
            "repo": "",
            "workspace": scrub_peer_text(row.get("source") or "Remote peer", secrets, limit=80),
            "tab": f"{name} · {sid}"[:80],
            "pane_label": "Read-only",
            "focused": False,
            "model": scrub_peer_text(clean_model_name(row.get("model")) or "", secrets, limit=60),
            "session_path": "",
            "has_question": has_question,
            "can_focus": False,
            "can_control": False,
        })
    return cards


def load_peer_registry() -> List[Dict[str, str]]:
    """Peers from the confined config.yaml, normalised and de-duplicated.

    A repeated name would otherwise cost a worker slot and re-query the same
    host every tick, so the (name, base) pair is collapsed here.
    """
    text = read_confined_text("config", os.path.join("~", ".hermes", "config.yaml"))
    peers: List[Dict[str, str]] = []
    for entry in parse_bot_peers(text):
        base = peer_base_url(entry.get("url") or "")
        if not base:
            continue
        row = {"name": entry["name"], "url": base, "base": base}
        if row not in peers:
            peers.append(row)
    return peers


def peer_request_target(base_url: str, path: str) -> Optional[str]:
    """Join an API path onto a validated base, refusing traversal in `path`.

    The traversal check runs on `path` BEFORE urljoin, not after: urljoin
    normalises `/api/sessions/../messages` down to `/api/messages` (measured), so
    a post-join check on the result never sees the traversal at all. A peer
    session id is charset-checked by the caller too — but this is the layer that
    holds when a caller forgets.
    """
    raw = str(path or "")
    if not raw or "\\" in raw:
        return None
    if any(segment in (".", "..") for segment in raw.split("/")):
        return None
    if not raw.startswith("/"):
        return None
    # A scheme-relative path ("//host/x") makes urljoin REPLACE the authority,
    # which would send the Bearer key to a host the user never registered. Refuse
    # it here and re-check the netloc after the join: this function is documented
    # as the layer that holds when a caller forgets, so it must hold for the form
    # a caller is most likely to forget.
    try:
        target = urljoin(base_url, raw)
    except ValueError:
        return None
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.username or parts.password:
        return None
    try:
        if parts.netloc.lower() != urlsplit(base_url).netloc.lower():
            return None
    except ValueError:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if any(segment in (".", "..") for segment in segments):
        return None
    return target


def peer_http_get(
    base_url: str,
    path: str,
    key: str,
    *,
    deadline: float,
    connect: Any = None,
) -> Optional[Any]:
    """GET one peer API path and return parsed JSON, or None.

    http.client, deliberately, not urllib:
      * it never follows a redirect, so the Authorization header can never be
        re-sent to a host the user never registered (a 3xx is a refusal);
      * it never consults HTTP_PROXY/HTTPS_PROXY/ALL_PROXY from the session env;
      * it exposes the socket, so the deadline can be ENFORCED.

    The deadline is a `timeout=` (per socket operation, renewed on every recv —
    useless alone against a peer that dribbles) PLUS a watchdog that closes the
    socket when the TOTAL per-peer budget is spent. A blown budget is a refusal,
    not a warning.

    `connect` is the injection seam for the tests: a stand-in class whose
    constructor takes (host, port, timeout=..., context=...) and whose connect()
    hands back a socket.
    """
    target = peer_request_target(base_url, path)
    if not target:
        return None
    parts = urlsplit(target)
    host = parts.hostname or ""
    port = parts.port
    body: Optional[bytes] = None
    conn = None
    watchdog: Optional[threading.Timer] = None
    try:
        if parts.scheme == "https":
            context = _peer_tls_context()
            if context is None:
                return None
            conn = (
                connect(host, port, timeout=PEER_REQ_BUDGET, context=context)
                if connect is not None
                else http.client.HTTPSConnection(host, port, timeout=PEER_REQ_BUDGET, context=context)
            )
        else:
            conn = (
                connect(host, port, timeout=PEER_REQ_BUDGET)
                if connect is not None
                else http.client.HTTPConnection(host, port, timeout=PEER_REQ_BUDGET)
            )
        # The response watchdog cannot interrupt getaddrinfo, and conn.connect()
        # resolves the name BEFORE any socket exists — a stalling resolver
        # measured 20 s against a 3 s deadline, with the watchdog already armed.
        #
        # A PRE-resolve alone was not enough: connect() then resolved the SAME
        # name a second time with no timeout, so a resolver that answered once
        # and then stalled still held the request open (re-measured at 20 s). So
        # resolve ONCE, under the budget, and hand connect() the literal ADDRESS:
        # no second lookup, nothing left for a blackholed resolver to do.
        connect_host = _peer_resolve_address(host, port, min(deadline - time.monotonic(), PEER_REQ_BUDGET))
        if not connect_host:
            return None
        # HTTPSConnection.connect() hands self.host to wrap_socket as
        # server_hostname, so overwriting conn.host with the literal address
        # would drop SNI and verify the certificate against the IP rather than
        # the hostname the user configured — breaking every https:// peer.
        # Instead: keep conn.host as the TLS identity and pin the ADDRESS for
        # the socket, which is exactly what HTTPSConnection._tunnel_host does
        # for the same reason.
        conn.port = port or (443 if parts.scheme == "https" else 80)
        # Redirect ONLY the TCP connect to the address we already resolved.
        # conn.host stays the hostname, which is what HTTPSConnection passes to
        # wrap_socket as server_hostname — so SNI and certificate verification
        # still check the name the user configured, not the IP. Overwriting
        # conn.host instead would silently break every https:// peer.
        conn._create_connection = (
            lambda address, timeout=None, source_address=None,
            _addr=connect_host, _port=conn.port: _connect_literal(_addr, _port, timeout, source_address)
        )
        conn.connect()
        # ENFORCE the total budget. A dribbling peer blocks inside
        # HTTPResponse.read() on the BUFFERED file object, and by then the
        # connection's own socket has already been consumed into the response
        # (conn.sock is None) — so closing conn does nothing and conn.sock.close()
        # never even runs. What unblocks it is shutdown(SHUT_RDWR) on the socket
        # behind response.fp, which makes the pending read raise instead of
        # waiting for the peer to finish. Measured: 25.1 s blocked -> 0.5 s
        # aborted with that shutdown, no change with a plain close().
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        response_holder: List[Any] = []  # filled once getresponse() returns
        watchdog = threading.Timer(remaining, _abort_response, args=(response_holder, conn))
        watchdog.daemon = True
        watchdog.start()
        # Parenthesise the fallback: `parts.path or "/" + q` parses as
        # `parts.path or ("/" + q)`, so the query was sent ONLY when the path was
        # empty — i.e. never, since every call starts with /api/. Both reviewers
        # caught this independently; the stub-based tests could not, because they
        # match the string handed to an injected fetch and never see the wire.
        request_target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn.request(
            "GET", request_target,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "Connection": "close",
            },
        )
        response = conn.getresponse()
        response_holder.append(response)
        # A 3xx is REFUSED, never followed.
        if not 200 <= response.status < 300:
            return None
        body = response.read(PEER_MAX_BYTES + 1)
        if body is None or len(body) > PEER_MAX_BYTES:
            return None  # pre-decode ceiling, before json.loads can allocate
    except Exception:
        return None  # unreachable, refused, timed out — all degrade to "no card"
    finally:
        if watchdog is not None:
            watchdog.cancel()
        _close_quietly(conn)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None


def _connect_literal(address: str, port: int, timeout: Optional[float], source_address: Any) -> socket.socket:
    """Open a TCP socket to an ALREADY-RESOLVED address, consulting no resolver.

    socket.create_connection((address, port)) looks the name up again, so a
    pinned literal still cost a second getaddrinfo call — the exact unbounded
    lookup this work set out to remove (measured: 2 calls, 20 s against a 3 s
    deadline). Building the socket here means the address we resolved under the
    budget is the address we connect to.
    """
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if timeout is not None:
            sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        sock.connect((address, port))
    except Exception:
        sock.close()
        raise
    return sock


def _peer_resolve_address(host: str, port: Optional[int], budget: float) -> Optional[str]:
    """Resolve the peer host to a literal address inside `budget`, or None.

    getaddrinfo is a blocking C call with no timeout, and http.client calls it
    inside connect() before any socket exists, so the response watchdog cannot
    cover it. This bounds it in a daemon thread and RETURNS the address, because
    a pre-resolve that only answered "does it resolve?" left connect() free to
    resolve the same name again — unbounded — which is what the round-2 review
    measured (20 s against a 3 s deadline on a second lookup).

    An IP literal is returned as-is: it needs no resolver at all.
    """
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host.strip("[]")
    except ValueError:
        pass
    outcome: List[str] = []

    def _resolve() -> None:
        try:
            infos = socket.getaddrinfo(host, port or 80, type=socket.SOCK_STREAM)
        except Exception:
            outcome.append("")
            return
        for family, _, _, _, sockaddr in infos:
            if family in (socket.AF_INET, socket.AF_INET6):
                outcome.append(str(sockaddr[0]))
                return
        outcome.append("")

    worker = threading.Thread(target=_resolve, daemon=True)
    worker.start()
    worker.join(max(0.0, budget))
    return outcome[0] if outcome else None


def _abort_response(holder: List[Any], conn: Any) -> None:
    """Interrupt a blocked response read when the total budget is spent.

    The handle that matters is the socket behind `response.fp`, not the
    connection: once getresponse() has run, the connection's own socket is gone
    and a plain close() leaves the pending read blocked. shutdown() is what makes
    it raise. Everything is best-effort — the read's own exception is what the
    caller sees, and the caller degrades to "no card" either way.
    """
    for response in holder:
        try:
            raw = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
    _close_quietly(conn)


def _close_quietly(conn: Any) -> None:
    """Close a connection without letting teardown raise out of a worker."""
    if conn is None:
        return
    try:
        sock = getattr(conn, "sock", None)
        if sock is not None:
            sock.close()  # unblocks a wedged recv from the watchdog thread
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def parse_peer_sessions(payload: Any) -> List[Dict[str, Any]]:
    """Rows from GET /api/sessions; anything not shaped like a list is empty."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    return [row for row in rows[:PEER_SESSIONS_QUERY * 4] if isinstance(row, dict)]


def parse_peer_message(payload: Any) -> Optional[Dict[str, Any]]:
    """The newest message row from GET /api/sessions/{id}/messages?order=latest."""
    if not isinstance(payload, dict):
        return None
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        return None
    for row in reversed(rows[:64]):
        if isinstance(row, dict):
            return row
    return None


def peer_session_is_active(row: Dict[str, Any], now: float) -> bool:
    """The dashboard's documented heuristic: not ended, touched recently.

    api_server exposes no turn-lease table, so "active" cannot be proven — the
    same 300 s heuristic the REST router stamps as `is_active` is the honest
    approximation, and a stalled turn therefore reads as working.
    """
    try:
        if row.get("ended_at") is not None:
            return False
        last = float(row.get("last_active") or 0)
    except (TypeError, ValueError):
        return False
    return last > 0 and (now - last) < PEER_ACTIVE_WINDOW


def peer_session_id(row: Dict[str, Any]) -> Optional[str]:
    """A session id safe to put in a URL path and in a card identity."""
    raw = str(row.get("id") or "").strip()
    if not raw or not PEER_SESSION_ID_RE.match(raw):
        return None
    # quote(safe="") leaves "." alone, so a bare ".." would encode to ".." and
    # build /api/sessions/../messages: the ENCODED value is what reaches the URL,
    # so that is what must be checked.
    encoded = quote(raw, safe="")
    if "/" in encoded or encoded in (".", ".."):
        return None
    return raw


def peer_target_id(peer: str, session_id: str) -> str:
    """Collision-proof, peer-qualified card identity."""
    return f"hermes-peer:{peer}:{session_id}"


def annotate_gateways_with_peers(
    gateways: List[Dict[str, str]],
    peer_status: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Link a Desktop gateway row to the peer that now has cards for it.

    Pure comparison of already-parsed data: a registry entry whose normalised
    URL equals a peer's base gains `peer` and `session_count`, so the reporter's
    existing `hermes_gateways` label is visibly the thing that now has cards.
    No new I/O, and a mismatch simply leaves the row untouched.
    """
    if not gateways or not peer_status:
        return gateways
    by_url = {
        str(row.get("url") or ""): row
        for row in peer_status
        if isinstance(row, dict) and row.get("url")
    }
    annotated: List[Dict[str, str]] = []
    for gateway in gateways:
        item = dict(gateway)
        base = peer_base_url(str(item.get("url") or ""))
        match = by_url.get(base or "")
        if match:
            item["peer"] = str(match.get("name") or "")[:64]
            item["session_count"] = str(_bounded_number(int(match.get("sessions") or 0)))
        annotated.append(item)
    return annotated


def peer_secret_env_name(peer: str) -> str:
    """The env name Hermes stores this peer's key under."""
    return f"HERMES_PEER_{peer.upper().replace('-', '_')}_KEY"


def load_peer_secret(peer: str) -> Optional[str]:
    """Load one peer's API key from the confined .env.

    split("\\n"), never splitlines(): \\x0b, \\x0c, \\x85, \\u2028 and \\u2029 would
    otherwise split one physical line into two and smuggle a second assignment
    past a per-line name check. The value is returned to the caller and NEVER
    written to os.environ — `launch_agent` spawns with no env= and would hand it
    to a child.
    """
    if not PEER_NAME_RE.match(str(peer or "")):
        return None
    want = peer_secret_env_name(peer)
    text = read_confined_text("env", os.path.join("~", ".hermes", ".env"))
    if not text:
        return None
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        if "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() != want:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # A short key would bypass scrub_peer_text's >=8 literal-redaction floor
        # and could then be echoed back into the always-on bar, so refuse it here
        # rather than relying on the scrubber.
        if not value or len(value) < 8:
            return None
        return value
    return None


def read_hermes_registry() -> Dict[str, Any]:
    text = read_confined_text("registry", HERMES_CONNECTIONS_PATH)
    if text is None:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def sanitize_hermes_registry_connections(registry: Any) -> List[Dict[str, str]]:
    """Expose Desktop gateway labels only; never read or forward OAuth secrets."""
    rows = registry.get("connections") if isinstance(registry, dict) else None
    if not isinstance(rows, list):
        return []
    result: List[Dict[str, str]] = []
    for row in rows[:64]:
        if not isinstance(row, dict):
            continue
        identity = str(row.get("id") or "").strip()
        label = str(row.get("label") or identity).strip()
        kind = str(row.get("kind") or "").strip()
        url = str(row.get("url") or "").strip()
        auth_mode = str(row.get("authMode") or "").strip()
        if not identity or not label or kind not in {"local", "remote", "cloud", "ssh"}:
            continue
        item = {"id": identity, "kind": kind, "label": label[:120]}
        if url.startswith(("http://", "https://")):
            item["url"] = url[:300]
        if auth_mode:
            item["auth_mode"] = auth_mode[:32]
        result.append(item)
    return result


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


def herdr_error_message(res: Optional[Dict[str, Any]]) -> Optional[str]:
    """Failure reason carried by a Herdr reply, or None when it succeeded.

    query_herdr_socket() collapses a missing socket, a timeout and a malformed
    reply into None, and Herdr reports a refused request as a JSON-RPC error
    object. Both mean the call did not take effect, so callers must not treat
    either as success.
    """
    if res is None:
        return "Herdr socket did not answer"
    err = res.get("error")
    if isinstance(err, dict):
        message = err.get("message") or err.get("code")
        return str(message) if message else "Herdr refused the request"
    if err:
        return str(err)
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
    if "mcp_death_supervisor.py" in cmd:
        return False
    return (
        "hermes_cli.main" in cmd
        or re.search(r"(?:^|\s)[^\s]*/hermes(?:\s+desktop)?(?:\s|$)", cmd) is not None
        or re.search(r"(?:^|\s)hermes(?:\s+desktop)?(?:\s|$)", cmd) is not None
    )


def hermes_profile_from_argv(argv: Optional[List[str]]) -> Optional[str]:
    """Return explicit Hermes profile from one process argv, if present."""
    args = list(argv or [])
    for index, value in enumerate(args):
        if value in ("--profile", "-p") and index + 1 < len(args):
            return args[index + 1].strip() or None
        if value.startswith("--profile="):
            return value.split("=", 1)[1].strip() or None
        if value.startswith("-p") and len(value) > 2:
            return value[2:].strip() or None
    return None


def normalize_hermes_agent(value: Any) -> Optional[str]:
    """Normalize Herdr labels such as ``hermes tui`` to one agent identity."""
    label = str(value or "").strip().lower()
    return "hermes" if label == "hermes" or label.startswith("hermes ") else None


def normalize_herdr_status(value: Any) -> str:
    """Map Herdr's status vocabulary to plugin status vocabulary."""
    status = str(value or "unknown").strip().lower()
    if status == "blocked":
        return "waiting"
    if status in {"busy", "running", "thinking", "generating"}:
        return "working"
    if status in {"prompt", "input"}:
        return "waiting"
    if status not in {"idle", "working", "waiting", "completed", "done", "finished", "error", "failed", "unknown"}:
        return "unknown"
    return {"done": "completed", "finished": "completed", "failed": "error"}.get(status, status)


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
# Bounds for the marker fallback of grok_sessions_for_cwd: how many claiming
# groups may be examined, how many sessions are taken from each (newest first),
# and how many sessions one lookup may return. Markers are attacker-controllable
# text, so a pile of groups all claiming the same cwd must not be able to spend
# the cycle budget (or take over the card) by volume; groups are ranked by the
# newest session they hold, and a group that yields no session does not consume
# the collection budget, so the freshest real session still wins.
GROK_MAX_MATCHED_GROUPS = 64
GROK_MAX_MATCHED_SESSIONS = 32
GROK_MAX_CWD_SESSIONS = 64
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

    # Fallback: groups whose `.cwd` marker claims this cwd (the slug+hash layout
    # Grok uses for very long paths). Marker content is local file text, so a group
    # that merely claims the directory must not crowd out the real session. Two
    # rules keep that true: groups are ranked by the newest session they hold, and
    # only sessions that actually pass the filters count — a group that yields
    # nothing (empty, or subagent-only) never consumes the collection budget, so a
    # pile of claimants cannot bury the real session behind the cap.
    candidates: List[Tuple[float, str]] = []
    for group in grok_group_dirs():
        if group in fast_folders or grok_group_cwd(group) not in wanted:
            continue
        newest = max(
            (grok_summary_mtime(os.path.join(group, name)) for name in grok_listdir(group)),
            default=0.0,
        )
        candidates.append((newest, group))
    candidates.sort(reverse=True)

    found: List[str] = []
    for _, group in candidates:
        if len(found) >= GROK_MAX_CWD_SESSIONS:
            break
        # Newest-first *before* truncating: taking the first N in name order would
        # keep whichever sessions happen to sort first, not the freshest ones.
        ranked = [n for n in grok_listdir(group) if grok_is_regular_file(os.path.join(group, n, "summary.json"))]
        ranked.sort(key=lambda n: grok_summary_mtime(os.path.join(group, n)), reverse=True)
        for name in ranked[:GROK_MAX_MATCHED_SESSIONS]:
            path = os.path.join(group, name)
            if grok_session_is_subagent(path):
                continue
            found.append(path)
            if len(found) >= GROK_MAX_CWD_SESSIONS:
                break
    found.sort(key=grok_summary_mtime, reverse=True)
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


def get_process_hermes_home(pid: int) -> Optional[str]:
    """Read non-secret HERMES_HOME marker for one local process."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as ef:
            for item in ef.read(64 * 1024).split(b"\x00"):
                if item.startswith(b"HERMES_HOME="):
                    value = item.split(b"=", 1)[1].decode("utf-8", errors="replace").strip()
                    return os.path.realpath(os.path.expanduser(value)) if value else None
    except Exception:
        pass
    return None


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

    # Owner and symlink checks: verify the directory is not a symlink, is owned
    # by the current user, and that none of its ancestor directories are symlinks.
    try:
        if os.path.islink(CLAUDE_HOOK_STATUS_DIR):
            return None
        if os.stat(CLAUDE_HOOK_STATUS_DIR).st_uid != os.getuid():
            return None
        # Check ancestors for symlinks
        check_dir = CLAUDE_HOOK_STATUS_DIR
        while check_dir != os.path.expanduser("~") and check_dir != "/":
            parent = os.path.dirname(check_dir)
            if os.path.islink(parent):
                return None
            try:
                if os.stat(parent).st_uid != os.getuid():
                    return None
            except Exception:
                return None
            check_dir = parent
        # Verify the file itself is not a symlink
        if os.path.islink(path):
            return None
    except Exception:
        return None

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


def get_all_hermes_dbs(hermes_home: Optional[str] = None, hermes_profile: Optional[str] = None) -> List[Tuple[str, str]]:
    """Discover databases for one Hermes home/profile, or all local homes."""
    root = os.path.realpath(os.path.expanduser(hermes_home)) if hermes_home else os.path.expanduser("~/.hermes")
    if hermes_profile:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", hermes_profile):
            return []
        if os.path.basename(root) == hermes_profile and os.path.basename(os.path.dirname(root)) == "profiles":
            profile_db = os.path.join(root, "state.db")
        else:
            profile_db = os.path.join(root, "profiles", hermes_profile, "state.db")
        return [(profile_db, hermes_profile)] if os.path.exists(profile_db) else []
    base_db = os.path.join(root, "state.db")
    if hermes_home:
        return [(base_db, os.path.basename(root) or "Default")] if os.path.exists(base_db) else []
    dbs = [(base_db, os.path.basename(root) or "Default")] if os.path.exists(base_db) else []
    for p in glob.glob(os.path.join(root, "profiles", "*/state.db")):
        dbs.append((p, os.path.basename(os.path.dirname(p))))
    return dbs


def extract_hermes_session_info(
    source_preference: Optional[str] = None,
    specific_session_id: Optional[str] = None,
    min_start_time: Optional[float] = None,
    hermes_home: Optional[str] = None,
    hermes_profile: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], bool]:
    """Extract prompt, model, provider, profile, message detail, and active status for Hermes.

    Scans default and profile state databases, inspects active turn leases, and evaluates
    message stream state to accurately determine working, waiting, or idle status.
    """
    now = time.time()
    all_dbs = get_all_hermes_dbs(hermes_home, hermes_profile)
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
            elif hermes_profile:
                cur.execute(
                    "SELECT id, source, title, model, billing_provider, profile_name, last_activity_at FROM sessions WHERE profile_name = ? ORDER BY last_activity_at DESC LIMIT 5;",
                    (hermes_profile,),
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


def _hermes_executable_token(cmd: str, argv: Optional[List[str]]) -> str:
    """Executable identity for one process: argv[0] wins, else first cmd token."""
    if argv:
        first_arg = (argv[0] or "").strip()
        if first_arg:
            return first_arg
    tokens = (cmd or "").split()
    return tokens[0] if tokens else ""


def is_hermes_desktop_gui_process(cmd: str, argv: Optional[List[str]] = None) -> bool:
    """True for Hermes Desktop Electron app/launcher processes (issue #24).

    Crashpad matches a bare "/Hermes" substring via its
    --database=.../Hermes/Crashpad argument but is a crash reporter, never a
    window or session — so the check is on the executable token, not the whole
    argument line. A missing/empty executable identity fails OPEN (not excluded).
    """
    text = cmd or ""
    if "--type=" in text:
        return False
    if "/Hermes" not in text:
        return False
    exe_name = os.path.basename(_hermes_executable_token(text, argv)).lower()
    if exe_name and "crashpad" in exe_name:
        return False
    return True


def hermes_desktop_gui_child_pids(pid: int) -> List[int]:
    """Direct children of one PID via a ppid-only /proc walk.

    Fail-OPEN by design: any error returns [] and the caller keeps the process
    instead of dropping it. Never raises.
    """
    try:
        entries = glob.glob("/proc/[0-9]*")
    except Exception:
        return []
    kids: List[int] = []
    for entry in entries:
        try:
            child = int(os.path.basename(entry))
            if child <= 0 or child == pid:
                continue
            with open(os.path.join(entry, "stat"), "r") as sf:
                content = sf.read()
            # comm may itself contain spaces/parens: ppid follows the last ")".
            tail = content[content.rfind(")") + 1:].split()
            if len(tail) < 2:
                continue
            if int(tail[1]) == pid:
                kids.append(child)
        except Exception:
            continue
    return kids


def scan_standalone_agents(herdr_server_pids: List[int], seen_cwds: Set[str], claimed_sessions: Set[str]) -> List[Dict[str, Any]]:
    """Discover AI agents running in normal terminal windows outside of Herdr."""
    standalone = []

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
            if is_in_herdr or is_broker_child or is_claude_helper_process(cmd, info.get("argv")) or is_grok_helper_process(cmd, info.get("argv")) or "omarchy-agent-usage" in cmd or "__omp_worker" in cmd or "gateway run" in cmd or "zygote" in cmd or "agent_ctl.py" in cmd:
                continue

            # Hermes CLI servers are local instance roots. They have no Hyprland
            # window, so keep them as process cards instead of dropping them.
            is_hermes_desktop_child = any(
                is_hermes_desktop_gui_process(a["cmd"], a.get("argv"))
                for a in ancestors
            )

            # Check if this is a Hermes Desktop GUI process
            if is_hermes_desktop_gui_process(cmd, info.get("argv")):
                if "hermes_desktop" in seen_cwds or is_in_herdr:
                    continue

                # Wayland launchers stay alive above the real app (issue #24):
                # a match with a matching child is the launcher, not a session.
                is_launcher = False
                try:
                    gui_kids = hermes_desktop_gui_child_pids(pid)
                except Exception:
                    gui_kids = []
                for kid in gui_kids:
                    try:
                        kinfo = get_process_info(kid)
                    except Exception:
                        continue
                    if kinfo and is_hermes_desktop_gui_process(kinfo["cmd"], kinfo.get("argv")):
                        is_launcher = True
                        break
                if is_launcher:
                    continue

                hermes_title, hermes_model, hermes_provider, hermes_profile, hermes_detail, hermes_status, hermes_has_q = extract_hermes_session_info(
                    source_preference="desktop", hermes_home=get_process_hermes_home(pid), hermes_profile=hermes_profile_from_argv(info.get("argv"))
                )
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

            # A Hermes `serve` process is an independent local instance but has
            # no terminal window. Keep one card per PID/profile.
            if first in ("python", "python3") and is_hermes_cli_process(cmd) and "serve --host" in cmd:
                profile = hermes_profile_from_argv(info.get("argv")) or "Default"
                hermes_home = get_process_hermes_home(pid)
                goal, model, _provider, _db_profile, detail, detected_status, has_question = extract_hermes_session_info(
                    source_preference=None,
                    min_start_time=get_process_start_time(pid),
                    hermes_home=hermes_home,
                    hermes_profile=profile,
                )
                process_status = detected_status or ("working" if info.get("state") in ("R", "D") else "idle")
                standalone.append({
                    "pane_id": f"process:hermes:{pid}",
                    "pid": pid,
                    "origin": "desktop" if is_hermes_desktop_child else "process",
                    "origin_label": "Hermes Desktop" if is_hermes_desktop_child else "Hermes CLI",
                    "agent": "hermes",
                    "agent_display": "Hermes Desktop" if is_hermes_desktop_child else "Hermes CLI",
                    "status": process_status,
                    "title": goal or f"Hermes {profile}",
                    "detail": detail or "Hermes server",
                    "cwd": shorten_path(info.get("cwd", "")),
                    "repo": "",
                    "workspace": "Local process",
                    "tab": f"{'Hermes Desktop' if is_hermes_desktop_child else 'Hermes CLI'} (PID {pid})",
                    "pane_label": f"Profile: {profile}",
                    "focused": False,
                    "model": "",
                    "session_path": "",
                    "has_question": False,
                    "can_focus": False,
                    "can_control": False,
                })
                continue

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
                        user_goal, model_name, _, _, detail_text, status_override, has_question = extract_hermes_session_info(
                            source_preference="cli", min_start_time=p_st, hermes_home=get_process_hermes_home(pid)
                        )
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


def _collect_peer_cards() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Probe registered remote Hermes peers, in parallel, under a hard budget.

    An empty roster is the common case (nobody registered a peer) and costs two
    confined reads only. Peers are queried concurrently with at most
    PEER_MAX_PEERS_PROBED of them, in config order; the rest are reported as
    skipped rather than dropped silently.
    """
    try:
        peers = load_peer_registry()
    except Exception:
        return [], []
    if not peers:
        return [], []
    selected = peers[:PEER_MAX_PEERS_PROBED]
    now = time.time()
    results: List[Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]] = [None] * len(selected)
    # NOT a `with` block: __exit__ calls shutdown(wait=True), which joins every
    # worker BEFORE the results are read, making the per-future timeout
    # decorative (a peer ignoring its deadline measured 16 s against a 3 s budget).
    # shutdown(wait=False, cancel_futures=True) lets the caller's timeout govern,
    # but ThreadPoolExecutor workers are NON-daemon: the interpreter joins them at
    # exit, so a parked peer used to hold the process (and its stdout pipe, which
    # Quickshell reads with waitForEnd:true) open long past the payload — measured
    # 25 s of process wall time AFTER all collector work was finished, which the
    # 18 s stall timer then killed, discarding the LOCAL cards too. main() now
    # exits explicitly, so nothing parked can delay or truncate the reply.
    tick_deadline = time.monotonic() + PEER_PEER_BUDGET + 1.0
    pool = ThreadPoolExecutor(max_workers=min(PEER_MAX_PEERS_PROBED, len(selected)))
    try:
        futures = {
            pool.submit(build_peer_agents, [entry], now): index
            for index, entry in enumerate(selected)
        }
        # ONE wall-clock deadline for the whole loop. Reading each future with
        # its own timeout serialised into N x 4 s (measured: 4 uncooperative peers
        # spent 16 s of the tick, against Panel.qml's 18 s stall timer), so one
        # slow peer was enough to lose the tick entirely.
        for future, index in futures.items():
            try:
                results[index] = future.result(timeout=max(0.0, tick_deadline - time.monotonic()))
            except Exception:
                results[index] = None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    cards: List[Dict[str, Any]] = []
    status_rows: List[Dict[str, Any]] = []
    for index, entry in enumerate(selected):
        result = results[index]
        if result is None:
            status_rows.append({
                "name": entry["name"][:64], "url": entry["url"][:PEER_URL_MAX],
                "reachable": False, "sessions": 0, "error": "probe did not finish in budget",
            })
            continue
        cards.extend(result[0])
        status_rows.extend(result[1])
    for entry in peers[PEER_MAX_PEERS_PROBED:]:
        status_rows.append({
            "name": entry["name"][:64], "url": entry["url"][:PEER_URL_MAX], "reachable": False,
            # NOT a failure: this peer was never probed, so the UI must not call it
            # unreachable. `reachable` alone cannot carry three meanings
            # (ok / failed / not-probed) without the footer lying about healthy peers.
            "state": "skipped",
            "sessions": 0, "error": f"skipped: over the {PEER_MAX_PEERS_PROBED}-peer per-tick budget",
        })
    return cards[:PEER_MAX_PEERS_PROBED * PEER_MAX_PEER_CARDS], status_rows


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
    unknown_count = 0
    active_agent_types = set()
    top_working_task = ""
    top_completed_task = ""
    seen_cwds: Set[str] = set()
    claimed_sessions: Set[str] = set()
    herdr_connected = False
    all_workspaces: List[str] = []

    def consume_herdr_snapshot(
        snap: Dict[str, Any],
        sess_name: str,
        sess_sock: str,
        remote_machine: Optional[Dict[str, str]] = None,
    ) -> None:
        """Parse one Herdr session snapshot and merge its agents into the results.

        Each Herdr session is an independent server with its own socket and
        ID namespace, so pane/tab/workspace maps are per-snapshot and every
        agent records the session it came from.
        """
        nonlocal working_count, completed_count, idle_count, waiting_count, unknown_count, top_working_task, top_completed_task

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
        screen_status_by_pane: Dict[str, Optional[str]] = {}
        if remote_machine:
            hermes_panes = [
                str(a.get("pane_id")) for a in raw_agents
                if normalize_hermes_agent(a.get("agent"))
            ]
            with ThreadPoolExecutor(max_workers=min(4, len(hermes_panes) or 1)) as screen_pool:
                reads = screen_pool.map(
                    lambda pane: (pane, query_remote_herdr_agent_read(remote_machine, pane)),
                    hermes_panes,
                )
                screen_status_by_pane = {
                    pane: hermes_screen_status(text) for pane, text in reads
                }
        for a in raw_agents:
            pane_id = a.get("pane_id")
            pane_info = panes_map.get(pane_id, {})

            raw_agent = (a.get("agent") or "agent").lower()
            agent_type = normalize_hermes_agent(raw_agent) or raw_agent
            raw_status = normalize_herdr_status(a.get("agent_status"))
            screen_status = None
            if agent_type == "hermes":
                screen_status = (
                    screen_status_by_pane.get(str(pane_id))
                    if remote_machine
                    else hermes_screen_status(query_herdr_pane_detection(str(pane_id), sess_sock))
                )

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

            if remote_machine:
                # Remote paths are not local paths. Never read them or select a local transcript.
                session_path = None
            elif not session_path or not os.path.exists(session_path):
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
            elif agent_type == "hermes" and not remote_machine:
                hermes_p_start = None
                hermes_profile = None
                for hp in glob.glob("/proc/[0-9]*"):
                    try:
                        hpid = int(os.path.basename(hp))
                        hinfo = get_process_info(hpid)
                        if hinfo and "hermes" in hinfo["cmd"].lower() and "gateway" not in hinfo["cmd"] and "zygote" not in hinfo["cmd"]:
                            if is_hermes_desktop and (is_hermes_desktop_gui_process(hinfo["cmd"], hinfo.get("argv")) or "hermes desktop" in hinfo["cmd"]):
                                hermes_p_start = get_process_start_time(hpid)
                                hermes_profile = hermes_profile_from_argv(hinfo.get("argv"))
                                break
                            elif not is_hermes_desktop and is_hermes_cli_process(hinfo["cmd"]):
                                hermes_p_start = get_process_start_time(hpid)
                                hermes_profile = hermes_profile_from_argv(hinfo.get("argv"))
                                break
                    except Exception:
                        pass
                if is_hermes_desktop:
                    user_goal, model_name, _, _, detail_text, status_override, has_question = extract_hermes_session_info(source_preference="desktop", min_start_time=hermes_p_start, hermes_profile=hermes_profile)
                else:
                    user_goal, model_name, _, _, detail_text, status_override, has_question = extract_hermes_session_info(source_preference="cli", min_start_time=hermes_p_start, hermes_profile=hermes_profile)
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
            # Hermes screen evidence outranks stale detector state; Herdr's agent_status remains fallback.
            if agent_type == "hermes" and screen_status:
                status_override = screen_status

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
            elif raw_status in ("blocked",):
                status = "waiting"
            elif raw_status in ("unknown",):
                status = "unknown"
            elif raw_status in ("error", "failed"):
                status = "error"
            else:
                # Herdr has no strong opinion (idle/empty): fall back to the
                # session tail, which distinguishes a finished task from a fresh prompt.
                status = status_override or "idle"

            origin = "herdr_remote" if remote_machine else ("herdr_desktop" if is_hermes_desktop else "herdr")
            origin_label = (
                f"Herdr · {remote_machine['label']}"
                if remote_machine
                else ("Herdr (Desktop)" if is_hermes_desktop else "Herdr")
            )
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
            elif status == "unknown":
                unknown_count += 1
            else:
                idle_count += 1
            detail_display = detail_text or (user_goal if user_goal and user_goal != effective_title else "") or clean_cwd

            agents_list.append(
                {
                    "pane_id": (
                        remote_herdr_target_id(remote_machine["id"], sess_name, str(pane_id))
                        if remote_machine
                        else f"herdr:{sess_name}|{pane_id}"
                    ),
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
                    "can_focus": not bool(remote_machine),
                    "can_control": not bool(remote_machine),
                }
            )

    for sess_name, sess_sock in herdr_session_sockets():
        resp = query_herdr_socket("session.snapshot", sock_path=sess_sock)
        if resp and "result" in resp and "snapshot" in resp["result"]:
            herdr_connected = True
            consume_herdr_snapshot(resp["result"]["snapshot"], sess_name, sess_sock)

    remote_machines = herdr_machine_list()
    # ponytail: eight concurrent SSH probes; add persistent async transports only
    # if fleet size makes one-shot polling measurably slow.
    with ThreadPoolExecutor(max_workers=min(8, len(remote_machines) or 1)) as pool:
        remote_results = list(pool.map(
            lambda machine: (machine, query_remote_herdr_snapshot(machine)),
            remote_machines,
        ))

    remote_machine_status = []
    for machine, response in remote_results:
        snapshot = response.get("result", {}).get("snapshot") if isinstance(response, dict) else None
        reachable = isinstance(snapshot, dict)
        remote_machine_status.append({
            "id": machine["id"],
            "label": machine["label"],
            "session": machine["session"],
            "reachable": reachable,
        })
        if reachable:
            herdr_connected = True
            consume_herdr_snapshot(snapshot, machine["session"], "", machine)

    # Orca-managed terminals run before the standalone process scan so they
    # can claim transcript sessions first; the standalone scan skips anything
    # whose session was claimed here, which dedupes agents visible to both.
    orca_agents = scan_orca_agents(claimed_sessions)

    # Remote Hermes peers run AFTER the local scans so a loopback peer's sessions
    # cannot be claimed here first (loopback peers are skipped anyway), and their
    # ids live in their own "hermes-peer:" namespace, so there is nothing to
    # dedupe against the local transcript claims.
    peer_cards, peer_status = _collect_peer_cards()

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

    for pc in peer_cards:
        if pc["status"] == "working":
            working_count += 1
            active_agent_types.add(pc["agent"])
            if not top_working_task:
                top_working_task = f"{pc['origin_label']}: {pc['title']}"
        elif pc["status"] == "waiting":
            waiting_count += 1
            active_agent_types.add(pc["agent"])
        elif pc["status"] == "completed":
            completed_count += 1
            if not top_completed_task:
                top_completed_task = f"{pc['origin_label']}: ✓ {pc['title']}"
        else:
            idle_count += 1
        agents_list.append(pc)

    def agent_sort_key(item: Dict[str, Any]) -> Tuple[int, int, str]:
        status_order = {"working": 0, "waiting": 1, "completed": 2, "error": 3, "unknown": 4, "idle": 5}
        # Peer cards carry agent "hermes", the same value as a local Hermes CLI
        # card, so without an origin tiebreaker they interleave with no grouping
        # cue. Remote sessions sort last within a status bucket.
        remote = 1 if item.get("origin") == "hermes_peer" else 0
        return (status_order.get(item["status"], 5), remote, item["agent"])

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
        "herdr_remote_machines": remote_machine_status,
        "hermes_gateways": annotate_gateways_with_peers(
            sanitize_hermes_registry_connections(read_hermes_registry()), peer_status
        ),
        "hermes_peers": peer_status,
        "orca_connected": bool(query_orca_terminals()),
        "summary": {
            "total": total,
            "working": working_count,
            "completed": completed_count,
            "idle": idle_count,
            "waiting": waiting_count,
            "unknown": unknown_count,
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
    if target_id.startswith("hermes-peer:"):
        # A remote peer's session has no window on this desktop to switch to.
        return {"ok": False, "error": "Remote Hermes peer sessions are read-only"}
    if target_id.startswith("herdr-remote:"):
        return {"ok": False, "error": "Remote Herdr targets are read-only"}

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


def close_hypr_window(addr: str, env: Optional[Dict[str, str]] = None) -> bool:
    """Close the Hyprland client at a validated address."""
    if not addr or not re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", str(addr)):
        return False
    try:
        subprocess.run(
            ["hyprctl", "dispatch", f'hl.dsp.window.close({{ window = "address:{addr}" }})'],
            env=env or get_hypr_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=0.5,
        )
        return True
    except Exception:
        return False


def agent_pids_in_window(win_pid: int) -> List[int]:
    """Recognized agent processes running under a terminal window's process.

    Standalone cards are keyed by window address, and the window's own process
    is the terminal emulator, so the agent to signal is a descendant of it.
    Every candidate still has to clear is_valid_agent_process(), which keeps
    the kill gate identical to the PID-addressed path.
    """
    if not isinstance(win_pid, int) or win_pid <= 1:
        return []
    pids = []
    for p in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(p))
        except ValueError:
            continue
        if pid == win_pid or not is_valid_agent_process(pid):
            continue
        if any(a["pid"] == win_pid for a in get_process_ancestors(pid)):
            pids.append(pid)
    return pids


def signal_agent_pids(pids: List[int]) -> Tuple[List[int], Optional[str]]:
    """SIGTERM each pid, reporting the first refusal."""
    killed = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            continue
        except Exception as e:
            return killed, f"Cannot signal PID {pid}: {e}"
    return killed, None


def kill_target(target_id: str) -> Dict[str, Any]:
    """Gracefully terminate an agent process or close a Herdr pane."""
    if not target_id:
        return {"ok": False, "error": "No target_id provided"}
    if target_id.startswith("hermes-peer:"):
        # Never signal anything for a remote peer's session: the id is a peer-
        # qualified string, and a crafted one must not reach os.kill via a
        # pid-bearing branch. Read-only by contract.
        return {"ok": False, "error": "Remote Hermes peer sessions are read-only"}
    if target_id.startswith("herdr-remote:"):
        return {"ok": False, "error": "Remote Herdr targets are read-only"}

    env = get_hypr_env()

    # 1. Standalone terminal window, the form scan_standalone_agents() emits
    if target_id.startswith("terminal:addr:"):
        addr = target_id[len("terminal:addr:"):]
        if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", addr):
            return {"ok": False, "error": "Invalid window address format"}
        target_win = next((c for c in get_hypr_clients() if c.get("address") == addr), None)
        if not target_win:
            return {"ok": False, "error": "Standalone terminal window not found"}
        pids = agent_pids_in_window(target_win.get("pid"))
        if not pids:
            return {"ok": False, "error": "No recognized agent process in that terminal window"}
        close_hypr_window(addr, env)
        killed, err = signal_agent_pids(pids)
        if err:
            return {"ok": False, "error": err, "killed_pids": killed}
        return {"ok": True, "pane_id": target_id, "killed_pids": killed}

    # 2. Standalone terminal addressed by agent PID
    if target_id.startswith("terminal:pid:"):
        pid_str = target_id.replace("terminal:pid:", "")
        try:
            pid = int(pid_str)
            if not is_valid_agent_process(pid):
                return {"ok": False, "error": f"PID {pid} is not a recognized agent process"}
            ancestors = get_process_ancestors(pid)
            ancestor_pids = [pid] + [a["pid"] for a in ancestors]
            matched_win = match_hypr_client_for_terminal(ancestor_pids, "", "")
            if matched_win and matched_win.get("address"):
                close_hypr_window(matched_win["address"], env)
            killed, err = signal_agent_pids([pid])
            if err:
                return {"ok": False, "error": err}
            return {"ok": True, "killed_pid": pid, "killed_pids": killed}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 3. Hermes Desktop window
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
            killed, err = signal_agent_pids([pid])
            if err:
                return {"ok": False, "error": err}
            return {"ok": True, "killed_pid": pid, "killed_pids": killed}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 4. Herdr pane (session-qualified targets look like herdr:<session>|<pane_id>)
    if target_id.startswith("herdr:"):
        parts = target_id.split("|", 1)
        sess_name = parts[0][len("herdr:"):]
        pane_target = parts[1] if len(parts) > 1 else target_id
        res = query_herdr_socket("pane.close", {"pane_id": pane_target}, sock_path=herdr_socket_for_session(sess_name))
        err = herdr_error_message(res)
        if err:
            return {"ok": False, "error": err, "pane_id": pane_target, "herdr_session": sess_name, "socket_res": res}
        return {"ok": True, "pane_id": pane_target, "herdr_session": sess_name, "socket_res": res}

    res = query_herdr_socket("pane.close", {"pane_id": target_id})
    err = herdr_error_message(res)
    if err:
        return {"ok": False, "error": err, "pane_id": target_id, "socket_res": res}
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
        "herdr_remote_machines": [],
        "hermes_gateways": [],
        "hermes_peers": [
            {
                "name": _clip_text(row.get("name"), 64),
                "url": _clip_text(row.get("url"), 300),
                "reachable": bool(row.get("reachable")),
                # Carried explicitly: the footer tells a skipped peer from a failed
                # one by this field, so dropping it here (the fallback rebuilds
                # each row field by field) would make healthy peers look broken.
                "state": _clip_text(row.get("state") or "", 16),
                "sessions": _bounded_number(row.get("sessions") or 0),
                "error": _clip_text(row.get("error"), 120),
            }
            for row in (payload.get("hermes_peers") or [])[:PEER_MAX_PEERS_PARSED]
            if isinstance(row, dict)
        ],
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


def _flush_and_exit(code: int = 0) -> None:
    """Flush stdout/stderr and leave immediately, skipping interpreter teardown.

    A peer probe can leave a ThreadPoolExecutor worker parked in a blocking
    resolver or socket read, and those workers are NON-daemon: the interpreter
    joins them at exit, so the process (and its stdout pipe, which Quickshell
    reads with waitForEnd:true) stays open long after the payload was written.
    The widget's 18 s stall timer then killed the tick and DISCARDED the reply,
    so a slow remote peer cost the local agents' cards as well (measured: 25 s of
    process wall time after all work had finished). Every guard has already run
    and the payload is on stdout by here, so teardown buys nothing.
    """
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("fetch", "status", "--json", "-j"):
        data = fetch_all_agents()
        print(dump_status_json(data))
        _flush_and_exit(0)
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
