#!/usr/bin/env python3
"""
agent_ctl.py - Backend collector and orchestrator controller for Omarchy Agent Orchestrator.
Connects via UNIX domain socket to Herdr (~/.config/herdr/herdr.sock), and also scans
standalone agent processes running in normal terminals (Foot, Alacritty, Kitty, Ghostty).
"""

import glob
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

HERDR_SOCK_PATH = os.path.expanduser(os.environ.get("HERDR_SOCKET_PATH", "~/.config/herdr/herdr.sock"))
OMP_SESSIONS_DIR = os.path.expanduser("~/.omp/agent/sessions")
HERMES_STATE_DB = os.path.expanduser("~/.hermes/state.db")


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


def get_process_info(pid: int) -> Optional[Dict[str, Any]]:
    """Read process cmdline, parent PID, and cwd from /proc."""
    try:
        proc_dir = f"/proc/{pid}"
        if not os.path.exists(proc_dir):
            return None
        with open(f"{proc_dir}/cmdline", "rb") as f:
            cmd = f.read().decode("utf-8", errors="replace").replace("\x00", " ").strip()
        with open(f"{proc_dir}/stat", "r") as f:
            stat_parts = f.read().split()
            ppid = int(stat_parts[3])
            state = stat_parts[2]
        cwd = os.path.realpath(f"{proc_dir}/cwd")
        return {"pid": pid, "ppid": ppid, "state": state, "cmd": cmd, "cwd": cwd}
    except Exception:
        return None


def get_herdr_server_pids() -> List[int]:
    """Find PID(s) of running Herdr server instances."""
    pids = []
    for p in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(p))
            info = get_process_info(pid)
            if info and "herdr server" in info["cmd"]:
                pids.append(pid)
        except Exception:
            continue
    return pids


def is_descendant_of(pid: int, target_pids: List[int], max_depth: int = 15) -> bool:
    """Check if process is a child/descendant of any target PIDs."""
    curr = pid
    visited = set()
    depth = 0
    while curr > 1 and curr not in visited and depth < max_depth:
        visited.add(curr)
        depth += 1
        info = get_process_info(curr)
        if not info:
            break
        if info["ppid"] in target_pids or curr in target_pids:
            return True
        curr = info["ppid"]
    return False


def find_latest_session_for_cwd(agent_type: str, cwd: str) -> Optional[str]:
    """Find the most recent session file matching a working directory."""
    if agent_type == "omp" and os.path.exists(OMP_SESSIONS_DIR):
        try:
            # Match folder name
            folder_part = os.path.basename(cwd.rstrip("/")) if cwd else ""
            pattern = os.path.join(OMP_SESSIONS_DIR, f"*{folder_part}*", "*.jsonl")
            matches = glob.glob(pattern)
            if not matches:
                matches = glob.glob(os.path.join(OMP_SESSIONS_DIR, "*", "*.jsonl"))
            if matches:
                matches.sort(key=os.path.getmtime, reverse=True)
                return matches[0]
        except Exception:
            pass
    return None


def extract_omp_task_from_session(session_path: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Extract user goal, latest activity/tool, and model from an OMP session .jsonl file.
    Returns (user_goal, latest_activity, model_name).
    """
    if not session_path or not os.path.exists(session_path):
        return None, None, None
    try:
        user_goal = None
        latest_activity = None
        model_name = None

        with open(session_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    msg_type = entry.get("type")
                    if msg_type == "message":
                        msg = entry.get("message", {})
                        role = msg.get("role")
                        if role == "user" and not user_goal:
                            content = msg.get("content")
                            if isinstance(content, list):
                                parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")]
                                user_goal = "".join(parts).strip()
                            elif isinstance(content, str):
                                user_goal = content.strip()
                        elif role == "assistant":
                            if "model" in entry:
                                model_name = entry["model"]
                            elif "model" in msg:
                                model_name = msg["model"]
                            content = msg.get("content", [])
                            if isinstance(content, list):
                                for item in reversed(content):
                                    if isinstance(item, dict):
                                        if item.get("type") == "toolCall":
                                            intent = item.get("intent") or item.get("name")
                                            if intent:
                                                latest_activity = f"Tool: {intent}"
                                                break
                                        elif item.get("type") == "text" and item.get("text"):
                                            txt = item["text"].strip().split("\n")[0]
                                            if txt and not txt.startswith("```"):
                                                latest_activity = txt[:80]
                                                break
                    elif msg_type == "custom":
                        c_type = entry.get("customType")
                        if c_type == "tool_execution_start":
                            data = entry.get("data", {})
                            intent = data.get("intent") or data.get("toolName")
                            if intent:
                                latest_activity = f"Running: {intent}"
                except Exception:
                    continue

        if user_goal:
            user_goal = " ".join(user_goal.split())
        return user_goal, latest_activity, model_name
    except Exception:
        return None, None, None


def extract_hermes_latest_session() -> Tuple[Optional[str], Optional[str], Optional[str], Optional[float]]:
    """Extract latest session info from Hermes state.db."""
    if not os.path.exists(HERMES_STATE_DB):
        return None, None, None, None
    try:
        conn = sqlite3.connect(HERMES_STATE_DB, timeout=0.5)
        cur = conn.cursor()
        cur.execute(
            "SELECT title, model, billing_provider, last_activity_at FROM sessions ORDER BY last_activity_at DESC LIMIT 1;"
        )
        row = cur.fetchone()
        conn.close()
        if row:
            title, model, provider, last_active = row
            return title or "", model or "hermes", provider or "", last_active
    except Exception:
        pass
    return None, None, None, None


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


def scan_standalone_agents(herdr_server_pids: List[int]) -> List[Dict[str, Any]]:
    """Discover AI agents running in normal terminal windows outside of Herdr."""
    standalone = []
    for p in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(p))
            info = get_process_info(pid)
            if not info:
                continue
            cmd = info["cmd"]
            if not cmd:
                continue
            tokens = cmd.split()
            first = os.path.basename(tokens[0])

            agent_type = None
            if first in ("omp", "hermes", "claude", "codex", "opencode", "cline", "cursor") or (first == "python" and "hermes" in cmd):
                if first == "python":
                    if "hermes_cli.main" in cmd or "hermes desktop" in cmd:
                        agent_type = "hermes"
                else:
                    agent_type = first

            if agent_type and not is_descendant_of(pid, herdr_server_pids):
                # Exclude internal daemon workers or subprocesses
                if "__omp_worker" in cmd or "gateway run" in cmd or "serve --host" in cmd or "zygote" in cmd or "agent_ctl.py" in cmd:
                    continue

                cwd = info["cwd"]
                repo_name = os.path.basename(cwd.rstrip("/")) if cwd else ""
                clean_cwd = shorten_path(cwd)

                session_path = find_latest_session_for_cwd(agent_type, cwd)
                user_goal = None
                latest_activity = None
                model_name = None

                if agent_type == "omp" and session_path:
                    user_goal, latest_activity, model_name = extract_omp_task_from_session(session_path)
                elif agent_type == "hermes":
                    hermes_title, hermes_model, _, _ = extract_hermes_latest_session()
                    user_goal = hermes_title
                    model_name = hermes_model

                effective_title = user_goal or (f"Working in {repo_name}" if repo_name and repo_name not in ("tmp", "~") else f"{agent_type.upper()} standalone session")
                detail_text = latest_activity or clean_cwd

                # Determine terminal emulator name if possible
                term_name = "terminal"
                p_info = get_process_info(info["ppid"])
                if p_info:
                    p_cmd = p_info["cmd"].lower()
                    for t in ("foot", "ghostty", "alacritty", "kitty", "wezterm", "gnome-terminal"):
                        if t in p_cmd:
                            term_name = t
                            break

                standalone.append({
                    "pane_id": f"standalone:pid:{pid}",
                    "pid": pid,
                    "is_standalone": True,
                    "agent": agent_type,
                    "agent_display": agent_type.upper() if agent_type in ("omp", "pi") else agent_type.capitalize(),
                    "status": "working" if info.get("state") in ("R", "D") else "idle",
                    "title": effective_title,
                    "detail": detail_text,
                    "cwd": clean_cwd,
                    "repo": repo_name,
                    "workspace": "Terminal",
                    "tab": f"{term_name.capitalize()} (PID {pid})",
                    "pane_label": f"Standalone {agent_type.upper()}",
                    "focused": False,
                    "model": model_name or "",
                    "session_path": session_path or "",
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

            if raw_status in ("working", "busy", "running"):
                status = "working"
                working_count += 1
                active_agent_types.add(agent_type)
            elif raw_status in ("waiting", "prompt", "input"):
                status = "waiting"
                waiting_count += 1
                active_agent_types.add(agent_type)
            elif raw_status in ("error", "failed"):
                status = "error"
            else:
                status = "idle"
                idle_count += 1

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

            agent_session = a.get("agent_session", {})
            session_path = agent_session.get("value") if isinstance(agent_session, dict) else None
            user_goal = None
            latest_activity = None
            model_name = None

            if agent_type == "omp" and session_path:
                user_goal, latest_activity, model_name = extract_omp_task_from_session(session_path)
            elif agent_type == "hermes":
                hermes_title, hermes_model, _, _ = extract_hermes_latest_session()
                if hermes_title:
                    user_goal = hermes_title
                    model_name = hermes_model

            is_generic_title = cleaned_title in (repo_name, "~", "tmp", "/tmp", "") or cleaned_title.startswith("/tmp") or cleaned_title.startswith("alberto@")

            if user_goal and (is_generic_title or len(cleaned_title) < 4):
                effective_title = user_goal
            elif cleaned_title and not is_generic_title:
                effective_title = cleaned_title
            elif user_goal:
                effective_title = user_goal
            elif pane_label:
                effective_title = pane_label
            elif repo_name and repo_name not in ("tmp", "~"):
                effective_title = f"Working in {repo_name}"
            else:
                effective_title = f"{agent_type.upper()} session"

            if status == "working" and not top_working_task:
                top_working_task = f"{agent_type.upper()}: {effective_title}"

            detail_text = latest_activity or (user_goal if user_goal and user_goal != effective_title else "") or clean_cwd

            agents_list.append(
                {
                    "pane_id": pane_id,
                    "is_standalone": False,
                    "agent": agent_type,
                    "agent_display": agent_type.upper() if agent_type in ("omp", "pi") else agent_type.capitalize(),
                    "status": status,
                    "title": effective_title,
                    "detail": detail_text,
                    "cwd": clean_cwd,
                    "repo": repo_name,
                    "workspace": workspace_name,
                    "tab": tab_name,
                    "pane_label": pane_label,
                    "focused": bool(a.get("focused")),
                    "model": model_name or "",
                    "session_path": session_path or "",
                }
            )

    # Scan and append standalone terminal agents
    standalone_agents = scan_standalone_agents(herdr_pids)
    for sa in standalone_agents:
        if sa["status"] == "working":
            working_count += 1
            active_agent_types.add(sa["agent"])
            if not top_working_task:
                top_working_task = f"{sa['agent_display']}: {sa['title']}"
        else:
            idle_count += 1
        agents_list.append(sa)

    def agent_sort_key(item: Dict[str, Any]) -> Tuple[int, int, str]:
        status_order = {"working": 0, "waiting": 1, "error": 2, "idle": 3}
        return (status_order.get(item["status"], 4), 0 if item.get("focused") else 1, item["agent"])

    agents_list.sort(key=agent_sort_key)

    total = len(agents_list)
    if working_count > 0:
        headline = top_working_task or f"{working_count} agent{'s' if working_count > 1 else ''} busy"
    elif total > 0:
        headline = f"{total} agent{'s' if total > 1 else ''} idle"
    else:
        headline = "No active agents"

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
        "workspaces": list(workspaces_map.values()),
    }


def focus_pane(target_id: str) -> Dict[str, Any]:
    """Focus a specific pane in Herdr or standalone terminal window."""
    if not target_id:
        return {"ok": False, "error": "No target_id provided"}

    if target_id.startswith("standalone:pid:"):
        pid = target_id.replace("standalone:pid:", "")
        try:
            subprocess.run(
                ["hyprctl", "dispatch", "focuswindow", f"pid:{pid}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=0.5,
            )
            return {"ok": True, "target": target_id, "mode": "standalone_pid"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # Send pane.focus to Herdr socket
    res = query_herdr_socket("pane.focus", {"pane_id": target_id})
    try:
        subprocess.run(
            ["hyprctl", "dispatch", "focuswindow", "class:^(org.omarchy.terminal|foot|alacritty|kitty|ghostty)$"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=0.5,
        )
    except Exception:
        pass

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

    if cmd in ("launch", "--launch", "-l"):
        agent_name = sys.argv[2] if len(sys.argv) > 2 else None
        result = launch_agent(agent_name)
        print(json.dumps(result))
        return

    print(json.dumps({"ok": False, "error": f"Unknown command {cmd}"}))
    sys.exit(1)


if __name__ == "__main__":
    main()
