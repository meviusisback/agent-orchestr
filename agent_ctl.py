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

KNOWN_TERMINALS = ("foot", "ghostty", "alacritty", "kitty", "wezterm", "gnome-terminal", "xterm")


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


def query_herdr_socket(method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    """Send JSON-RPC request to Herdr socket and return parsed response."""
    if not os.path.exists(HERDR_SOCK_PATH):
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(HERDR_SOCK_PATH)
        req_id = f"orchestr:{int(time.time() * 1000)}"
        payload = {"id": req_id, "method": method, "params": params or {}}
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))

        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
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
    if ws_id is not None:
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
    if addr:
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
    # 1. Direct open file descriptor
    open_s = get_process_open_session(pid)
    if open_s and open_s not in claimed_sessions:
        return open_s

    # 2. If no open session, check if there is an unclaimed session modified around/after process started
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
        if len(line) > max_len:
            return line[:max_len - 1].rstrip() + "…"
        return line
    return ""


def clean_user_prompt(text: str) -> str:
    """Clean user prompt text by stripping system wrappers, XML tags, and extra whitespace."""
    if not text:
        return ""
    cleaned = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
    cleaned = re.sub(r"<system-directive>.*?</system-directive>", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"<[^>]+>", "", cleaned).strip()
    cleaned = re.sub(r"^#+\s*", "", cleaned).strip()
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

        with open(session_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    msg_type = entry.get("type")

                    if "model" in entry and entry["model"]:
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

        status_override = None
        detail = None
        has_question = False

        if pending_ask_question:
            status_override = "waiting"
            detail = f"❓ {pending_ask_question}"
            has_question = True
        elif pending_tool:
            status_override = "working"
            t_name, t_intent = pending_tool
            detail = f"Running: {t_intent}" if t_intent else f"Running tool: {t_name}"
        elif last_assistant_text:
            status_override = "idle"
            detail = extract_first_line(last_assistant_text)
            has_question = "?" in (detail[-40:] if detail else "")
        return latest_user_prompt, detail, clean_model_name(model_name), status_override, has_question
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
            conn = sqlite3.connect(db_path, timeout=0.5)
            cur = conn.cursor()

            # Find active unexpired turn leases with alive holder PIDs
            active_leases: Dict[str, Dict[str, Any]] = {}
            try:
                cur.execute("SELECT conversation_id, holder, acquired_at, expires_at FROM session_turn_leases;")
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

    # Open the winning DB and session to extract detailed messages
    try:
        conn = sqlite3.connect(best["db_path"], timeout=0.5)
        cur = conn.cursor()
        session_id = best["session_id"]

        # Latest human prompt
        latest_user_prompt = None
        cur.execute(
            "SELECT content, display_kind FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC;",
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

        # Latest assistant response / tool status
        cur.execute(
            "SELECT role, content, tool_name, tool_calls, finish_reason FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1;",
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
                        status = "idle"
                        detail = first_line
                else:
                    if is_active:
                        status = "working"
                        detail = "Thinking…"
                    else:
                        status = "idle"
                        detail = "Ready for prompt"
            elif role == "tool":
                detail = f"Tool result: {tool_name or 'completed'}"
                status = "working" if is_active else "idle"
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
                "SELECT content FROM messages WHERE session_id = ? AND role = 'assistant' AND content IS NOT NULL ORDER BY id DESC LIMIT 1;",
                (session_id,)
            )
            ast_row = cur.fetchone()
            if ast_row and ast_row[0]:
                first_line = extract_first_line(ast_row[0])
                if first_line:
                    detail = first_line

        conn.close()

        effective_prompt = latest_user_prompt or best["title"] or ""
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
            elif first in ("claude", "codex", "opencode", "cline", "cursor"):
                agent_type = first
            elif first == "python" and ("hermes_cli.main" in cmd or "hermes desktop" in cmd):
                agent_type = "hermes"

            if agent_type:
                term_name = None
                for anc in ancestors:
                    anc_cmd = anc["cmd"].lower()
                    for t in KNOWN_TERMINALS:
                        if t in anc_cmd:
                            term_name = t
                            break
                    if term_name:
                        break

                if not term_name:
                    continue

                cwd = info["cwd"]
                repo_name = os.path.basename(cwd.rstrip("/")) if cwd else ""
                clean_cwd = shorten_path(cwd)

                matched_client = match_hypr_client_for_terminal(ancestor_pids, cwd, agent_type)
                if matched_client:
                    ws_id = str(matched_client.get("workspace", {}).get("name", matched_client.get("workspace", {}).get("id", "1")))
                    workspace_name = f"Desktop {ws_id}"
                    tab_name = f"{term_name.capitalize()} (Desktop {ws_id})"
                    window_addr = matched_client.get("address", "")
                    pane_id = f"terminal:addr:{window_addr}" if window_addr else f"terminal:pid:{pid}"
                else:
                    workspace_name = "Terminal"
                    tab_name = f"{term_name.capitalize()} (PID {pid})"
                    pane_id = f"terminal:pid:{pid}"

                session_path = find_session_for_process(agent_type, pid, cwd, claimed_sessions)
                user_goal = None
                detail_text = None
                model_name = None
                status_override = None
                has_question = False

                if session_path:
                    claimed_sessions.add(session_path)
                    if agent_type == "omp":
                        user_goal, detail_text, model_name, status_override, has_question = extract_omp_task_from_session(session_path)
                    elif agent_type == "hermes":
                        user_goal, model_name, _, _, detail_text, status_override, has_question = extract_hermes_session_info(source_preference="cli")
                else:
                    detail_text = "Ready for prompt"
                    status_override = "idle"

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
    idle_count = 0
    waiting_count = 0
    active_agent_types = set()
    top_working_task = ""
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
                if is_hermes_desktop:
                    user_goal, model_name, _, _, detail_text, status_override, has_question = extract_hermes_session_info(source_preference="desktop")
                else:
                    user_goal, model_name, _, _, detail_text, status_override, has_question = extract_hermes_session_info(source_preference="cli")
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
            # Determine effective status
            if status_override == "waiting":
                status = "waiting"
            elif raw_status in ("waiting", "prompt", "input"):
                status = "waiting"
            elif status_override == "working":
                status = "working"
            elif agent_type == "hermes" and status_override:
                status = status_override
            elif raw_status in ("working", "busy", "running"):
                if status_override == "idle" and not a.get("focused"):
                    status = "idle"
                else:
                    status = "working"
            elif raw_status in ("error", "failed"):
                status = "error"
            else:
                status = "idle"
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
                    "model": model_name or "",
                    "session_path": session_path or "",
                    "has_question": has_question,
                }
            )

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
        else:
            idle_count += 1
        agents_list.append(sa)

    def agent_sort_key(item: Dict[str, Any]) -> Tuple[int, int, str]:
        status_order = {"working": 0, "waiting": 1, "error": 2, "idle": 3}
        return (status_order.get(item["status"], 4), 0 if item.get("focused") else 1, item["agent"])

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
    elif total > 0:
        headline = f"{total} agent{'s' if total > 1 else ''} idle"
    else:
        headline = "No active agents"

    all_workspaces = list(workspaces_map.values())
    for sa in standalone_agents:
        ws = sa.get("workspace")
        if ws and ws not in all_workspaces:
            all_workspaces.append(ws)

    return {
        "ok": True,
        "connected": herdr_connected,
        "summary": {
            "total": total,
            "working": working_count,
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
        except Exception:
            standalone_win = None
        if not standalone_win:
            standalone_win = next(
                (
                    c
                    for c in clients
                    if c.get("class") in ("com.mitchellh.ghostty", "org.omarchy.terminal", "foot", "alacritty", "kitty")
                    and "MAIN" not in c.get("title", "")
                    and "herdr" not in c.get("title", "").lower()
                ),
                None,
            )
        if standalone_win:
            focus_hypr_window(standalone_win)
            return {
                "ok": True,
                "target": target_id,
                "focused_window": standalone_win.get("title", ""),
                "workspace": standalone_win.get("workspace", {}).get("id"),
            }
        return {"ok": False, "error": "Standalone terminal window not found"}

    # 3. Herdr pane
    sock_path = os.path.expanduser("~/.config/herdr/herdr.sock")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(sock_path)
        req = {"id": "focus:exec", "method": "pane.focus", "params": {"pane_id": target_id}}
        s.sendall((json.dumps(req) + "\n").encode())
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
            ancestors = get_process_ancestors(pid)
            clients = get_hypr_clients()
            ancestor_pids = [pid] + [a["pid"] for a in ancestors]
            matched_win = match_hypr_client_for_terminal(ancestor_pids, "", "")
            if matched_win and matched_win.get("address"):
                try:
                    win_addr = matched_win["address"]
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
            for p in [pid] + [a["pid"] for a in ancestors[:3]]:
                try:
                    os.kill(p, signal.SIGTERM)
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
