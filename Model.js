// Model.js - Helper functions for Agent Orchestrator QML UI.

function statusColor(status, fg, accent, urgent) {
  var s = String(status || "").toLowerCase()
  if (s === "completed" || s === "done") {
    return "#10B981"
  }
  if (s === "working" || s === "busy" || s === "running") {
    return accent || "#38BDF8"
  }
  if (s === "waiting" || s === "prompt" || s === "input") {
    return "#F59E0B"
  }
  if (s === "error" || s === "failed") {
    return urgent || "#EF4444"
  }
  return Qt.darker(fg || "#FFFFFF", 1.8)
}

function statusBadgeText(status) {
  var s = String(status || "").toLowerCase()
  if (s === "completed" || s === "done") return "DONE ✓"
  if (s === "working") return "ACTIVE"
  if (s === "waiting") return "PROMPT"
  if (s === "error") return "ERROR"
  return "IDLE"
}

function originColor(origin) {
  var o = String(origin || "").toLowerCase()
  if (o === "orca") return "#22D3EE"
  if (o.indexOf("desktop") >= 0) return "#F59E0B"
  if (o === "terminal") return "#38BDF8"
  return "#A855F7"
}

function originBadgeText(origin) {
  var o = String(origin || "").toLowerCase()
  if (o === "orca") return "ORCA"
  if (o === "herdr_desktop") return "HERDR · GUI"
  if (o === "desktop") return "DESKTOP APP"
  if (o === "terminal") return "TERMINAL"
  return "HERDR"
}

function originIcon(origin) {
  // Return "" so the panel falls back to the SVG brand mark (originIconPath)
  // instead of a possibly-missing font glyph.
  var o = String(origin || "").toLowerCase()
  if (o === "orca") return ""
  if (o.indexOf("desktop") >= 0) return "󰨇"
  if (o === "terminal") return ""
  return "󰘦"
}

function originIconPath(origin) {
  var o = String(origin || "").toLowerCase()
  if (o === "orca") return "assets/icons/orca.svg"
  return ""
}

function originSummaryText(agents) {
  var list = agents || []
  if (list.length === 0) return "No active agent instances"

  var herdrCount = 0
  var terminalCount = 0
  var desktopCount = 0
  var orcaCount = 0

  for (var i = 0; i < list.length; i++) {
    var a = list[i]
    var o = String(a.origin || "").toLowerCase()
    if (o === "orca") {
      orcaCount++
    } else if (o === "desktop") {
      desktopCount++
    } else if (o === "terminal") {
      terminalCount++
    } else {
      herdrCount++
    }
  }

  var parts = []
  if (herdrCount > 0) {
    parts.push(herdrCount + " on Herdr")
  }
  if (terminalCount > 0) {
    parts.push(terminalCount + (terminalCount === 1 ? " on terminal" : " on terminals"))
  }
  if (orcaCount > 0) {
    parts.push(orcaCount + (orcaCount === 1 ? " on Orca" : " on Orca"))
  }
  if (desktopCount > 0) {
    parts.push(desktopCount + (desktopCount === 1 ? " desktop app" : " desktop apps"))
  }

  return parts.join(" · ")
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
  var waiting = Number(summary.waiting) || 0
  var completed = Number(summary.completed) || 0
  var mode = String(displayMode || "Icon").toLowerCase()

  if (mode === "compact") {
    if (waiting > 0) return total + " ag · " + waiting + " prompt"
    if (working > 0) return total + " ag · " + working + " busy"
    if (completed > 0) return total + " ag · " + completed + " done"
    return total + " agents"
  }

  if (mode === "status") {
    if (summary.headline && (working > 0 || waiting > 0 || completed > 0)) {
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
  var completed = Number(summary.completed) || 0
  var idle = Number(summary.idle) || 0
  var waiting = Number(summary.waiting) || 0

  if (total === 0) return "Agent Orchestrator: No active agents"
  var parts = []
  if (working > 0) parts.push(working + " working")
  if (waiting > 0) parts.push(waiting + " awaiting input")
  if (completed > 0) parts.push(completed + " completed")
  if (idle > 0) parts.push(idle + " idle")
  return "Agent Orchestrator · " + parts.join(", ") + " (" + total + " total)"
}
