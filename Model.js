// Model.js - Helper functions for Agent Orchestrator QML UI.

function statusColor(status, fg, accent, urgent) {
  var s = String(status || "").toLowerCase()
  if (s === "working" || s === "busy" || s === "running") {
    return accent || "#10B981" // Active Green / Accent
  }
  if (s === "waiting" || s === "prompt" || s === "input") {
    return "#F59E0B" // Amber warning
  }
  if (s === "error" || s === "failed") {
    return urgent || "#EF4444" // Urgent Red
  }
  return Qt.darker(fg || "#FFFFFF", 1.8) // Neutral / Dim
}

function statusBadgeText(status) {
  var s = String(status || "").toLowerCase()
  if (s === "working") return "ACTIVE"
  if (s === "waiting") return "PROMPT"
  if (s === "error") return "ERROR"
  return "IDLE"
}

function agentDisplayName(agent) {
  var a = String(agent || "").toLowerCase()
  if (a === "omp" || a === "pi") return "OMP"
  if (a === "herdr") return "Herdr"
  if (a === "hermes") return "Hermes"
  if (a === "claude") return "Claude"
  if (a === "codex") return "Codex"
  if (a === "opencode") return "OpenCode"
  return a ? a.charAt(0).toUpperCase() + a.slice(1) : "Agent"
}

function agentIconPath(agent) {
  var a = String(agent || "").toLowerCase()
  var known = ["omp", "hermes", "herdr", "claude", "codex", "opencode"]
  if (known.indexOf(a) >= 0) {
    return "assets/icons/" + a + ".svg"
  }
  return "assets/icons/generic.svg"
}

function truncateText(text, maxLen) {
  if (!text) return ""
  var str = String(text).trim()
  var limit = Number(maxLen) || 45
  if (str.length <= limit) return str
  return str.slice(0, limit - 1).trim() + "…"
}

function formatBarHeadline(summary, displayMode, maxLen) {
  if (!summary) return "Agents"
  var total = Number(summary.total) || 0
  var working = Number(summary.working) || 0
  var mode = String(displayMode || "Icon").toLowerCase()

  if (mode === "compact") {
    if (working > 0) return total + " ag · " + working + " busy"
    return total + " agents"
  }

  if (mode === "status") {
    if (summary.headline && working > 0) {
      return truncateText(summary.headline, maxLen || 40)
    }
    if (total > 0) return total + " agents idle"
    return "Agents idle"
  }

  return ""
}

function getTooltipText(summary) {
  if (!summary) return "Agent Orchestrator"
  var total = Number(summary.total) || 0
  var working = Number(summary.working) || 0
  var idle = Number(summary.idle) || 0
  var waiting = Number(summary.waiting) || 0

  if (total === 0) return "Agent Orchestrator: No active agents"
  var parts = []
  if (working > 0) parts.push(working + " working")
  if (waiting > 0) parts.push(waiting + " awaiting input")
  if (idle > 0) parts.push(idle + " idle")
  return "Agent Orchestrator · " + parts.join(", ") + " (" + total + " total)"
}
