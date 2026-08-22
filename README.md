# Agent Orchestrator for Omarchy

Real-time status, active tasks, and one-click workspace switching for AI coding agents (**Herdr**, **OMP**, **Hermes**, **Claude**, **Codex**, **OpenCode**) in the Omarchy bar.

![Agent Orchestrator](preview.png)

## Features

- **Live Status & Active Tasks**: Connects directly to the Herdr daemon socket (`~/.config/herdr/herdr.sock`), OMP session transcripts (`~/.omp/agent/sessions/`), and Hermes SQLite state (`~/.hermes/state.db`) in under 5ms.
- **One-Click Workspace Switching**: Click on any agent card to instantly focus its specific pane in Herdr and bring the terminal window to the front.
- **Visual Status Bar Display**:
  - **`Icon` Mode**: Robot glyph with a live pulsing activity badge when agents are working.
  - **`Status` Mode**: Real-time ticker showing the top active agent and task description.
  - **`Compact` Mode**: Quick summary (e.g. `4 agents · 1 busy`).
- **Interactive Popup Panel**:
  - Hero header with live counts and quick action buttons (Refresh, New Agent).
  - Quick filter tabs: `All`, `Working`, `Idle`.
  - Rich cards displaying agent brand marks, model tags, user goals, live tool execution details, repositories, and workspace breadcrumbs.

## Installation

Symlink or clone this repository into your Omarchy plugins directory:

```bash
mkdir -p ~/.config/omarchy/plugins
ln -sfn ~/repo/agent-orchestr ~/.config/omarchy/plugins/meviusisback.agent-orchestr
```

Then add `"meviusisback.agent-orchestr"` to your status bar layout in `~/.config/omarchy/shell.json`:

```json
{
  "id": "meviusisback.agent-orchestr",
  "barDisplay": "Icon",
  "refreshIntervalSec": 3
}
```

And restart the shell:

```bash
omarchy restart shell
```

## Settings

| Key | Default | Description |
|---|---|---|
| `barDisplay` | `"Icon"` | `"Icon"`, `"Status"`, or `"Compact"` |
| `refreshIntervalSec` | `3` | Polling frequency in seconds |
| `showIdleInBar` | `false` | Whether to display badge count when all agents are idle |
| `maxTaskLength` | `45` | Maximum characters shown in the bar status ticker |

## License

MIT
