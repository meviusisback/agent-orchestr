import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Effects
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "meviusisback.agent-orchestr"
  ipcTarget: "meviusisback.agent-orchestr"
  manageIpc: false

  // Bar slot sizing driven by activeItem
  implicitWidth: root.barShowsText ? Math.max(dataButton.implicitWidth, Style.space(130)) : button.implicitWidth
  implicitHeight: button.implicitHeight

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.6)
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color track: Style.selectedFillFor(foreground, Color.accent)

  // Configuration settings
  readonly property int refreshIntervalSec: Math.max(1, Number(root.setting("refreshIntervalSec", 3)) || 3)
  readonly property string barDisplay: String(root.setting("barDisplay", "Icon"))
  readonly property bool showIdleInBar: Boolean(root.setting("showIdleInBar", false))
  readonly property int maxTaskLength: Math.max(20, Number(root.setting("maxTaskLength", 45)) || 45)

  readonly property bool barShowsText: barDisplay.toLowerCase() === "status" || barDisplay.toLowerCase() === "compact"

  // Live state
  property var rawData: ({
    ok: false,
    connected: false,
    summary: { total: 0, working: 0, idle: 0, waiting: 0, active_agents: [], headline: "Loading…" },
    agents: [],
    workspaces: []
  })

  property var summary: rawData && rawData.summary ? rawData.summary : ({ total: 0, working: 0, idle: 0, waiting: 0, headline: "Offline" })
  property var agents: rawData && rawData.agents ? rawData.agents : []
  property bool loading: false
  property string selectedFilter: "all" // "all" | "working" | "herdr" | "external"
  property string lastFocusedPane: ""

  readonly property var filteredAgents: {
    var list = root.agents || []
    if (root.selectedFilter === "working") {
      return list.filter(function(a) { return a.status === "working" || a.status === "waiting" })
    }
    if (root.selectedFilter === "herdr") {
      return list.filter(function(a) { return a.origin === "herdr" || a.origin === "herdr_desktop" })
    }
    if (root.selectedFilter === "external") {
      return list.filter(function(a) { return a.origin === "terminal" || a.origin === "desktop" })
    }
    return list
  }

  readonly property int herdrCount: (root.agents || []).filter(function(a) { return a.origin === "herdr" || a.origin === "herdr_desktop" }).length
  readonly property int externalCount: (root.agents || []).filter(function(a) { return a.origin === "terminal" || a.origin === "desktop" }).length

  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

  function scriptPath() {
    return Qt.resolvedUrl("agent_ctl.py").toString().replace(/^file:\/\//, "")
  }

  function fetchStatus() {
    if (!fetchProc.running) {
      root.loading = true
      fetchProc.running = true
    }
  }

  function focusPane(paneId) {
    if (!paneId) return
    root.lastFocusedPane = paneId
    focusProc.command = ["python3", root.scriptPath(), "focus", paneId]
    focusProc.running = true
    root.close()
  }

  function launchAgent(agentName) {
    launchProc.command = ["python3", root.scriptPath(), "launch", agentName || ""]
    launchProc.running = true
    root.close()
  }

  // Periodic status poll
  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.fetchStatus()
  }

  // Live pulsing animation for working agents
  property real pulseOpacity: 1.0
  SequentialAnimation on pulseOpacity {
    running: root.summary.working > 0
    loops: Animation.Infinite
    NumberAnimation { to: 0.35; duration: 750; easing.type: Easing.InOutQuad }
    NumberAnimation { to: 1.0; duration: 750; easing.type: Easing.InOutQuad }
  }

  // Background processes
  Process {
    id: fetchProc
    command: ["python3", root.scriptPath(), "status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.loading = false
        var output = text || ""
        try {
          var data = JSON.parse(output)
          root.rawData = data
        } catch (e) {
          // ignore transient parse error
        }
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.loading = false
    }
  }

  Process {
    id: focusProc
  }

  Process {
    id: launchProc
  }

  IpcHandler {
    enabled: true
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { root.fetchStatus() }
    function focus(paneId: string): void { root.focusPane(paneId) }
  }

  // ------------------------------------------------------------- Bar Button (Icon Mode)
  BarIconButton {
    id: button
    anchors.fill: parent
    visible: !root.barShowsText
    bar: root.bar
    text: "󰚩" // Nerd font robot / orchestrator glyph
    tooltipText: Model.getTooltipText(root.summary)
    active: root.summary.working > 0
    activeColor: root.accent
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) {
        root.fetchStatus()
      } else if (buttonCode === Qt.MiddleButton && root.agents.length > 0) {
        root.focusPane(root.agents[0].pane_id)
      } else {
        root.toggle()
      }
    }

    // Active Badge on top of BarIconButton
    Rectangle {
      id: iconBadge
      visible: root.summary.working > 0 || (root.showIdleInBar && root.summary.total > 0)
      anchors.top: parent.top
      anchors.right: parent.right
      anchors.topMargin: Style.space(2)
      anchors.rightMargin: Style.space(2)
      implicitWidth: badgeInnerRow.implicitWidth + Style.space(6)
      implicitHeight: Style.space(13)
      radius: Style.space(7)
      color: root.summary.working > 0 ? root.accent : root.alpha(root.foreground, 0.2)

      RowLayout {
        id: badgeInnerRow
        anchors.centerIn: parent
        spacing: Style.space(2)

        Text {
          text: String(root.summary.working > 0 ? root.summary.working : root.summary.total)
          font.family: root.fontFamily
          font.pixelSize: Style.space(8)
          font.bold: true
          color: root.summary.working > 0 ? Color.background : root.foreground
        }
      }
    }
  }

  // ------------------------------------------------------------- Bar Button (Data / Status Mode)
  Item {
    id: dataButton
    anchors.fill: parent
    visible: root.barShowsText

    function triggerPress(buttonCode) {
      if (root.bar) root.bar.hideTooltip(dataButton)
      if (buttonCode === Qt.RightButton) {
        root.fetchStatus()
      } else if (buttonCode === Qt.MiddleButton && root.agents.length > 0) {
        root.focusPane(root.agents[0].pane_id)
      } else {
        root.toggle()
      }
    }

    implicitWidth: chipIcon.implicitWidth + chipLabel.implicitWidth + (chipBadge.visible ? chipBadge.implicitWidth + Style.space(4) : 0) + Style.space(18)
    implicitHeight: Math.max(chipIcon.implicitHeight, chipLabel.implicitHeight)

    RowLayout {
      anchors.centerIn: parent
      spacing: Style.space(6)

      Text {
        id: chipIcon
        text: "󰚩"
        font.family: root.fontFamily
        font.pixelSize: Style.font.size.barIcon || Style.space(14)
        color: root.summary.working > 0 ? root.accent : root.foreground
      }

      Text {
        id: chipLabel
        text: Model.formatBarHeadline(root.summary, root.barDisplay, root.maxTaskLength)
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption || Style.space(11)
        font.weight: root.summary.working > 0 ? Font.DemiBold : Font.Normal
        color: root.summary.working > 0 ? root.foreground : root.dim
        elide: Text.ElideRight
        Layout.maximumWidth: Style.space(220)
      }

      Rectangle {
        id: chipBadge
        visible: root.summary.working > 0
        implicitWidth: Style.space(6)
        implicitHeight: Style.space(6)
        radius: Style.space(3)
        color: root.accent
        opacity: root.pulseOpacity
      }
    }

    MouseArea {
      id: dataMouse
      anchors.fill: parent
      acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
      hoverEnabled: true
      onClicked: function(mouse) { dataButton.triggerPress(mouse.button) }
      onContainsMouseChanged: {
        if (!root.bar) return
        if (containsMouse) root.bar.showTooltip(dataButton, Model.getTooltipText(root.summary))
        else root.bar.hideTooltip(dataButton)
      }
      Component.onCompleted: if (root.bar && root.bar.registerClickTarget) root.bar.registerClickTarget(dataButton)
      Component.onDestruction: if (root.bar && root.bar.unregisterClickTarget) root.bar.unregisterClickTarget(dataButton)
    }
  }

  // ------------------------------------------------------------- Popup Panel
  PopupCard {
    id: popup
    anchorItem: root.barShowsText ? dataButton : button
    bar: root.bar
    owner: root
    open: root.opened
    contentWidth: Style.space(450)
    contentHeight: Style.space(580)

    ColumnLayout {
      anchors.fill: parent
      spacing: Style.space(12)

      // --------------------------------------------------------- Header Row
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(14)

        Text {
          text: "󰚩"
          color: root.summary.working > 0 ? root.accent : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.space(32)
          Layout.alignment: Qt.AlignVCenter
        }

        ColumnLayout {
          Layout.fillWidth: true
          Layout.alignment: Qt.AlignVCenter
          spacing: Style.space(2)

          Text {
            text: "Agent Orchestrator"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }

          Text {
            text: {
              if (!root.rawData.connected && root.summary.total === 0) return "No agents detected"
              var w = root.summary.working || 0
              var i = root.summary.idle || 0
              var t = root.summary.total || 0
              return w + " working · " + i + " idle across " + t + " active instance" + (t === 1 ? "" : "s")
            }
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
            Layout.fillWidth: true
          }
        }

        // Refresh Button
        BorderSurface {
          implicitWidth: Style.space(32)
          implicitHeight: Style.space(32)
          radius: Style.cornerRadius
          color: refreshMouse.containsMouse ? Style.normalFillFor(root.foreground, root.accent) : "transparent"
          borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
          Layout.alignment: Qt.AlignVCenter

          Text {
            anchors.centerIn: parent
            text: "󰑐"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.space(14)
            rotation: root.loading ? 360 : 0
            Behavior on rotation { NumberAnimation { duration: 600; easing.type: Easing.InOutCubic } }
          }

          MouseArea {
            id: refreshMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.fetchStatus()
          }
        }
      }

      // --------------------------------------------------------- Filter Tabs
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(6)

        Repeater {
          model: [
            { id: "all", label: "All (" + (root.summary.total || 0) + ")" },
            { id: "working", label: "Working (" + (root.summary.working || 0) + ")" },
            { id: "herdr", label: "Herdr (" + root.herdrCount + ")" },
            { id: "external", label: "Terminal / App (" + root.externalCount + ")" }
          ]

          Rectangle {
            required property var modelData
            Layout.fillWidth: true
            implicitHeight: Style.space(26)
            radius: Style.space(13)
            color: root.selectedFilter === modelData.id ? root.alpha(root.accent, 0.2) : root.alpha(root.foreground, 0.06)
            border.width: 1
            border.color: root.selectedFilter === modelData.id ? root.accent : "transparent"

            Text {
              anchors.centerIn: parent
              text: modelData.label
              font.family: root.fontFamily
              font.pixelSize: Style.space(10)
              font.weight: root.selectedFilter === modelData.id ? Font.DemiBold : Font.Normal
              color: root.selectedFilter === modelData.id ? root.accent : root.foreground
            }

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: root.selectedFilter = modelData.id
            }
          }
        }
      }

      // --------------------------------------------------------- Agent Cards List
      ScrollView {
        Layout.fillWidth: true
        Layout.fillHeight: true
        clip: true
        ScrollBar.vertical.policy: ScrollBar.AsNeeded

        ListView {
          id: agentListView
          model: root.filteredAgents
          spacing: Style.space(8)
          boundsBehavior: Flickable.StopAtBounds

          delegate: Rectangle {
            required property var modelData
            required property int index

            width: agentListView.width
            implicitHeight: cardContent.implicitHeight + Style.space(16)
            radius: Style.radius.panelItem || Style.space(8)

            color: {
              if (cardMouseArea.containsPress) return root.track
              if (cardMouseArea.containsMouse) return root.alpha(root.foreground, 0.1)
              if (modelData.status === "working") return root.alpha(root.accent, 0.08)
              return root.alpha(root.foreground, 0.04)
            }
            border.width: modelData.status === "working" ? 1.5 : 1
            border.color: {
              if (modelData.status === "working") return root.alpha(root.accent, root.pulseOpacity * 0.8)
              if (modelData.focused) return root.alpha(root.foreground, 0.3)
              return root.alpha(root.foreground, 0.08)
            }

            Behavior on color { ColorAnimation { duration: 120 } }

            ColumnLayout {
              id: cardContent
              anchors.fill: parent
              anchors.margins: Style.space(10)
              spacing: Style.space(6)

              // Card Header: Brand Icon + Name + Origin Pill + Model + Status Badge + Focus Button
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(8)

                // Agent Brand SVG Mark
                Image {
                  source: Qt.resolvedUrl(Model.agentIconPath(modelData.agent))
                  sourceSize.width: Style.space(20)
                  sourceSize.height: Style.space(20)
                  Layout.preferredWidth: Style.space(20)
                  Layout.preferredHeight: Style.space(20)
                  fillMode: Image.PreserveAspectFit
                }

                // Agent Name
                Text {
                  text: modelData.agent_display || Model.agentDisplayName(modelData.agent)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.size.medium || Style.space(13)
                  font.bold: true
                  color: root.foreground
                }

                // Origin Pill (Herdr vs Terminal vs Desktop)
                Rectangle {
                  implicitWidth: originRow.implicitWidth + Style.space(8)
                  implicitHeight: Style.space(16)
                  radius: Style.space(4)
                  color: root.alpha(Model.originColor(modelData.origin), 0.15)
                  border.width: 1
                  border.color: root.alpha(Model.originColor(modelData.origin), 0.4)

                  RowLayout {
                    id: originRow
                    anchors.centerIn: parent
                    spacing: Style.space(3)
                    Text {
                      text: Model.originIcon(modelData.origin)
                      font.family: root.fontFamily
                      font.pixelSize: Style.space(8)
                      color: Model.originColor(modelData.origin)
                    }
                    Text {
                      text: Model.originBadgeText(modelData.origin)
                      font.family: root.fontFamily
                      font.pixelSize: Style.space(8)
                      font.bold: true
                      color: Model.originColor(modelData.origin)
                    }
                  }
                }

                // Model Chip (if present)
                Rectangle {
                  visible: Boolean(modelData.model)
                  implicitWidth: modelText.implicitWidth + Style.space(8)
                  implicitHeight: Style.space(16)
                  radius: Style.space(4)
                  color: root.alpha(root.foreground, 0.1)

                  Text {
                    id: modelText
                    anchors.centerIn: parent
                    text: modelData.model || ""
                    font.family: root.fontFamily
                    font.pixelSize: Style.space(9)
                    color: root.dim
                  }
                }

                Item { Layout.fillWidth: true }

                // Status Pill
                Rectangle {
                  implicitWidth: statusPillRow.implicitWidth + Style.space(8)
                  implicitHeight: Style.space(18)
                  radius: Style.space(9)
                  color: root.alpha(Model.statusColor(modelData.status, root.foreground, root.accent, root.urgent), 0.2)
                  border.width: 1
                  border.color: Model.statusColor(modelData.status, root.foreground, root.accent, root.urgent)

                  RowLayout {
                    id: statusPillRow
                    anchors.centerIn: parent
                    spacing: Style.space(4)

                    Rectangle {
                      width: Style.space(5)
                      height: Style.space(5)
                      radius: Style.space(3)
                      color: Model.statusColor(modelData.status, root.foreground, root.accent, root.urgent)
                      opacity: modelData.status === "working" ? root.pulseOpacity : 1.0
                    }

                    Text {
                      text: Model.statusBadgeText(modelData.status)
                      font.family: root.fontFamily
                      font.pixelSize: Style.space(9)
                      font.bold: true
                      color: Model.statusColor(modelData.status, root.foreground, root.accent, root.urgent)
                    }
                  }
                }

                // Interactive Focus Button
                Rectangle {
                  implicitWidth: focusBtnText.implicitWidth + Style.space(10)
                  implicitHeight: Style.space(20)
                  radius: Style.space(5)
                  color: focusMouse.containsMouse ? root.alpha(root.accent, 0.3) : root.alpha(root.foreground, 0.12)
                  border.width: 1
                  border.color: focusMouse.containsMouse ? root.accent : root.alpha(root.foreground, 0.2)

                  RowLayout {
                    id: focusBtnText
                    anchors.centerIn: parent
                    spacing: Style.space(3)
                    Text {
                      text: "Focus"
                      font.family: root.fontFamily
                      font.pixelSize: Style.space(10)
                      font.weight: Font.Medium
                      color: root.foreground
                    }
                    Text {
                      text: "↗"
                      font.family: root.fontFamily
                      font.pixelSize: Style.space(10)
                      color: root.accent
                    }
                  }

                  MouseArea {
                    id: focusMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.focusPane(modelData.pane_id)
                  }
                }
              }

              // Task Title
              Text {
                Layout.fillWidth: true
                text: modelData.title || "Active agent session"
                font.family: root.fontFamily
                font.pixelSize: Style.font.size.small || Style.space(12)
                font.weight: modelData.status === "working" ? Font.DemiBold : Font.Normal
                color: root.foreground
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
              }

              // Activity Detail (if running tool or prompt)
              Text {
                visible: Boolean(modelData.detail) && modelData.detail !== modelData.title
                Layout.fillWidth: true
                text: modelData.detail || ""
                font.family: root.fontFamily
                font.pixelSize: Style.space(10)
                font.italic: true
                color: root.summary.working > 0 && modelData.status === "working" ? root.accent : root.dim
                elide: Text.ElideRight
              }

              // Breadcrumbs / Location Metadata
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(6)

                // Repo / Directory Pill
                Rectangle {
                  implicitWidth: repoText.implicitWidth + Style.space(10)
                  implicitHeight: Style.space(16)
                  radius: Style.space(4)
                  color: root.alpha(root.foreground, 0.08)

                  RowLayout {
                    id: repoText
                    anchors.centerIn: parent
                    spacing: Style.space(3)
                    Text {
                      text: "📁"
                      font.pixelSize: Style.space(9)
                    }
                    Text {
                      text: modelData.repo || modelData.cwd || "~"
                      font.family: root.fontFamily
                      font.pixelSize: Style.space(9)
                      color: root.foreground
                    }
                  }
                }

                // Workspace & Tab Location
                Text {
                  Layout.fillWidth: true
                  text: {
                    var parts = []
                    if (modelData.workspace) parts.push(modelData.workspace)
                    if (modelData.tab) parts.push(modelData.tab)
                    if (modelData.pane_label && modelData.pane_label !== modelData.tab) parts.push(modelData.pane_label)
                    return parts.join(" > ")
                  }
                  font.family: root.fontFamily
                  font.pixelSize: Style.space(9)
                  color: root.dim
                  elide: Text.ElideRight
                }
              }
            }

            // Click entire card to focus pane / window
            MouseArea {
              id: cardMouseArea
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              hoverEnabled: true
              onClicked: root.focusPane(modelData.pane_id)
            }
          }
        }
      }

      // --------------------------------------------------------- Footer
      Rectangle {
        Layout.fillWidth: true
        implicitHeight: Style.space(28)
        radius: Style.radius.panelItem || Style.space(6)
        color: root.alpha(root.foreground, 0.04)

        RowLayout {
          anchors.fill: parent
          anchors.leftMargin: Style.space(8)
          anchors.rightMargin: Style.space(8)

          // Socket / Service connection indicator
          RowLayout {
            spacing: Style.space(5)
            Rectangle {
              width: Style.space(6)
              height: Style.space(6)
              radius: Style.space(3)
              color: root.rawData.connected ? "#10B981" : "#38BDF8"
            }
            Text {
              text: root.rawData.connected ? "Herdr + Terminal Live" : "Standalone Scanner Active"
              font.family: root.fontFamily
              font.pixelSize: Style.space(9)
              color: root.dim
            }
          }

          Item { Layout.fillWidth: true }

          // Keyboard hint
          Text {
            text: "Click card to focus · Right-click bar to refresh"
            font.family: root.fontFamily
            font.pixelSize: Style.space(9)
            color: root.dim
          }
        }
      }
    }
  }
}
