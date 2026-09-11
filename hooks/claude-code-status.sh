#!/bin/bash
# Writes a live status file per Claude Code session for this plugin to
# consume for agent_type == "claude" (see read_claude_hook_status() in
# agent_ctl.py). Claude Code keeps no JSONL fd open the way OMP does and
# exposes no local session API the way Hermes does, so without this the
# plugin can only see that a `claude` process exists, never its actual
# working/waiting/completed state.
#
# Install: register this script in ~/.claude/settings.json under the hook
# events handled below (see README for the exact JSON), then `chmod +x` it.
set -uo pipefail

STATE_DIR="$HOME/.local/state/omarchy/agents/claude-status"

# ── Owner and symlink checks ─────────────────────────────────────────────────
# Verify the status directory (and all ancestors) are owned by the current user
# and are not symlinks. This prevents an attacker from creating a symlink at a
# parent path and redirecting our writes to an arbitrary location.
validate_path() {
  local check_path="$1"
  local owner_now
  owner_now="$(stat -c '%U' "$check_path" 2>/dev/null)" || exit 1
  if [ "$owner_now" != "$(id -un)" ]; then
    # Directory is owned by someone else — refuse to use it
    return 1
  fi
  if [ -L "$check_path" ]; then
    # Directory is a symlink — refuse to use it
    return 1
  fi
  return 0
}

# Also validate that ancestor paths are not symlinks
validate_ancestors() {
  local dir="$1"
  local current="$dir"
  while [ "$current" != "/" ] && [ "$current" != "$HOME" ]; do
    current="$(dirname "$current")"
    if [ -L "$current" ]; then
      return 1
    fi
    local owner_now
    owner_now="$(stat -c '%U' "$current" 2>/dev/null)" || return 1
    if [ "$owner_now" != "$(id -un)" ]; then
      return 1
    fi
  done
  return 0
}

mkdir -p "$STATE_DIR" || exit 0
# Validate the status directory
if ! validate_path "$STATE_DIR" || ! validate_ancestors "$STATE_DIR"; then
  exit 0
fi

input="$(cat)"

get() { printf '%s' "$input" | jq -r "$1 // empty" 2>/dev/null; }

event="$(get '.hook_event_name')"
cwd="$(get '.cwd')"
tool_name="$(get '.tool_name')"
message="$(get '.message')"

# argv[0]'s basename, the way agent_ctl.py itself identifies a claude
# process. /proc/<pid>/comm can be the wrapper binary (node) instead.
proc_argv0_name() {
  local argv0=""
  IFS= read -r -d '' argv0 < "/proc/$1/cmdline" 2>/dev/null
  [ -n "$argv0" ] && printf '%s' "${argv0##*/}"
}

# The hook runs as a child process of the claude CLI (not as claude itself),
# so walk up /proc ancestry to find the actual claude PID the plugin's /proc
# scan will see. cwd alone isn't a usable key: multiple concurrent sessions
# can share a working directory.
find_claude_pid() {
  local pid="$PPID"
  local depth=0
  while [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$pid" != "1" ] && [ "$depth" -lt 10 ]; do
    if [ "$(proc_argv0_name "$pid")" = "claude" ] || [ "$(cat "/proc/$pid/comm" 2>/dev/null)" = "claude" ]; then
      printf '%s' "$pid"
      return 0
    fi
    local ppid
    ppid="$(awk '$1=="PPid:"{print $2}' "/proc/$pid/status" 2>/dev/null)"
    [ -z "$ppid" ] && break
    pid="$ppid"
    depth=$((depth + 1))
  done
  return 1
}

# Drop files whose session died without firing SessionEnd (SIGKILL, crash).
# Keeps the directory bounded and stops a dead PID's last status from being
# read back if the kernel recycles that PID onto a new claude process.
prune_dead_sessions() {
  local f pid
  for f in "$STATE_DIR"/*.json; do
    [ -e "$f" ] || continue
    pid="$(basename "$f" .json)"
    case "$pid" in
      *[!0-9]*) continue ;;
    esac
    [ -d "/proc/$pid" ] || rm -f "$f"
  done
}

claude_pid="$(find_claude_pid)" || exit 0

status_file="$STATE_DIR/${claude_pid}.json"

case "$event" in
  UserPromptSubmit)
    status="working"; detail="Processing prompt" ;;
  PreToolUse)
    status="working"; detail="Running: ${tool_name:-tool}" ;;
  Notification)
    status="waiting"; detail="${message:-Needs your input}" ;;
  Stop|SubagentStop)
    status="completed"; detail="Ready for prompt" ;;
  SessionEnd)
    rm -f "$status_file"
    prune_dead_sessions
    exit 0 ;;
  *)
    exit 0 ;;
esac

# Written via a temp file so the plugin, which polls concurrently, never
# reads a half-written JSON document.
# Validate that the status file is not a symlink before overwriting it.
if [ -L "$status_file" ]; then
  rm -f "$status_file"
fi
tmp_file="$(mktemp "${status_file}.XXXXXX")" || exit 0
jq -n \
  --arg status "$status" \
  --arg detail "$detail" \
  --arg cwd "$cwd" \
  --argjson updated "$(date +%s)" \
  '{status: $status, detail: $detail, cwd: $cwd, updated: $updated}' \
  > "$tmp_file" && mv "$tmp_file" "$status_file" || rm -f "$tmp_file"

exit 0