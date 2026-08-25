#!/usr/bin/env python3
"""
agent_ctl.py - Backend collector and orchestrator controller for Omarchy Agent Orchestrator.
Discovers and manages AI agents across:
1. Herdr workspaces & panes (Herdr daemon socket + process tree correlation)
2. Standard terminal windows (Foot, Alacritty, Kitty, Ghostty)
3. Hermes Desktop GUI instances (Electron app)
"""

import glob
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

HERDR_SOCK_PATH = os.path.expanduser(os.environ.get("HERDR_SOCKET_PATH", "~/.config/herdr/herdr.sock"))
OMP_SESSIONS_DIR = os.path.expanduser("~/.omp/agent/sessions")
HERMES_STATE_DB = os.path.expanduser("~/.hermes/state.db")

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
        if parts[0] in ("google-antigravity", "openrouter", "anthropic", "openai", "deepseek", "groq", "together"):
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


def query_herdr_socket(method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    """Send JSON-RPC request to Herdr socket and return parsed response."""
    if not os.path.exists(HERDR_SOCK_PATH):
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(min(timeout, 0.5))
        s.connect(HERDR_SOCK_PATH)
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
    if first in ("omp", "pi", "claude", "codex", "opencode", "cline", "cursor", "agy"):
        return True
    if first in ("python", "python3") and ("hermes_cli.main" in cmd or "hermes desktop" in cmd or "hermes" in cmd):
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
            cmd = cf.read().decode("utf-8", errors="replace").replace("\x00", " ").strip()
        cwd = os.path.realpath(f"{proc_dir}/cwd")
        return {"pid": pid, "ppid": ppid, "tty": tty_nr, "state": state, "cmd": cmd, "cwd": cwd}
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


def find_session_for_process(agent_type: str, pid: int, cwd: str, claimed_sessions: Set[str]) -> Optional[str]:
    """Find active or recent session file strictly belonging to this specific process."""
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
        line = " ".join(line.split())
        if not line:
            continue
        line = redact_secrets(line)
        if len(line) > max_len:
            return line[:max_len - 1].rstrip() + "…"
        return line
    return ""


def clean_user_prompt(text: str) -> str:
    """Clean user prompt text by stripping system wrappers, XML tags, secrets, and extra whitespace."""
    if not text:
        return ""
    cleaned = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
    cleaned = re.sub(r"<system-directive>.*?</system-directive>", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"<[^>]+>", "", cleaned).strip()
    cleaned = re.sub(r"^#+\s*", "", cleaned).strip()
    cleaned = redact_secrets(cleaned)
    return " ".join(cleaned.split())


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
}

# Bare shells / system programs that mean "no agent in this terminal".
_ORCA_BARE_SHELLS = {
    "zsh", "bash", "sh", "fish", "nushell", "nu", "dash", "ksh",
    "alberto@omarchy", "omarchy", "shell", "sudo", "pacman", "vim", "nvim",
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
}

# Agent names looked up literally (as words) in the preview when the title's
# model token yields no hint. Ordered most-specific first.
_ORCA_PREVIEW_AGENT_NAMES = ("hermes", "opencode", "gemini", "claude", "codex", "omp", "agy", "pi")


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


def query_orca_terminals(timeout: float = 4.0) -> List[Dict[str, Any]]:
    """Return the live terminals known to the Orca runtime, cached briefly.

    Single bounded subprocess invocation. On success the result is cached for
    _ORCA_CACHE_TTL seconds. On failure (CLI missing, timeout, non-JSON) we do
    NOT poison the cache with an empty list — instead we return the most recent
    good result (or [] on the very first call) and leave the cache untouched, so
    a transient Orca CLI hang can't blank out the roster for the whole TTL.
    """
    now = time.monotonic()
    if now - _ORCA_CACHE["ts"] < _ORCA_CACHE_TTL:
        return _ORCA_CACHE["terminals"]

    terminals: List[Dict[str, Any]] = []
    try:
        proc = subprocess.run(
            [ORCA_CLI, "terminal", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            payload = json.loads(proc.stdout)
            if isinstance(payload, dict) and payload.get("ok"):
                result = payload.get("result") or {}
                raw = result.get("terminals")
                if isinstance(raw, list):
                    terminals = [t for t in raw if isinstance(t, dict)]
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
            if is_in_herdr or is_broker_child or "omarchy-agent-usage" in cmd or "__omp_worker" in cmd or "gateway run" in cmd or "serve --host" in cmd or "zygote" in cmd or "agent_ctl.py" in cmd:
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
            elif first in ("claude", "codex", "opencode", "cline", "cursor"):
                agent_type = first
            elif first == "python" and ("hermes_cli.main" in cmd or "hermes desktop" in cmd):
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
                session_path = find_session_for_process(agent_type, pid, cwd, claimed_sessions)
                user_goal = None
                detail_text = None
                model_name = None
                status_override = None
                has_question = False

                if session_path:
                    claimed_sessions.add(session_path)
                    if agent_type == "omp":
                        if os.path.exists(session_path):
                            user_goal, detail_text, model_name, status_override, has_question = extract_omp_task_from_session(session_path)
                        else:
                            model_name = get_omp_default_model()
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
    herdr_pids = get_herdr_server_pids()
    resp = query_herdr_socket("session.snapshot")

    workspaces_map = {}
    tabs_map = {}
    panes_map = {}
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

    herdr_connected = bool(resp and "result" in resp and "snapshot" in resp["result"])

    if herdr_connected:
        snap = resp["result"]["snapshot"]

        for ws in snap.get("workspaces", []):
            workspaces_map[ws.get("workspace_id")] = ws.get("label") or f"Workspace {ws.get('number', 1)}"

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
                            elif not is_hermes_desktop and ("hermes_cli" in hinfo["cmd"] or hinfo["cmd"].endswith("hermes")):
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
                    "pane_id": pane_id,
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
    all_workspaces = list(workspaces_map.values())
    for sa in standalone_agents + orca_agents:
        ws = sa.get("workspace")
        if ws and ws not in all_workspaces:
            all_workspaces.append(ws)

    return {
        "ok": True,
        "connected": herdr_connected,
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
        "workspaces": all_workspaces,
    }


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
    # 3. Herdr pane
    sock_path = os.path.expanduser("~/.config/herdr/herdr.sock")
    if os.path.exists(sock_path):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(sock_path)
            req = {"jsonrpc": "2.0", "id": "focus:exec", "method": "pane.focus", "params": {"pane_id": target_id}}
            s.sendall((json.dumps(req) + "\n").encode())
            try:
                s.recv(4096)
            except Exception:
                pass
            s.close()
        except Exception:
            pass
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
            "pane_id": target_id,
            "focused_window": herdr_win.get("title", ""),
            "workspace": herdr_win.get("workspace", {}).get("id"),
        }

    return {"ok": True, "pane_id": target_id}

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

    # 3. Herdr pane
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


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("fetch", "status", "--json", "-j"):
        data = fetch_all_agents()
        print(json.dumps(data, indent=2))
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
