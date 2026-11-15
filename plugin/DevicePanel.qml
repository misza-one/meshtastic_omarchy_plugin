import QtQuick
import qs.Ui as Ui
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

PanelScrollView {
  id: view
  required property var radio
  required property var strings
  readonly property var device: radio.device
  readonly property var diagnostics: radio.diagnostics
  readonly property string unknown: strings.t("device.unknown")
  property bool technicalExpanded: false
  property string selectedPeerId: ""

  function label(key) { return strings.t("device.ui." + key) }
  function row(key, text) { return {label: label(key), value: text} }
  function value(text) { return typeof text === "string" && text !== "" ? text : unknown }
  function number(value, digits, unit) {
    if (typeof value !== "number" || !Number.isFinite(value)) return unknown
    return Commons.I18n.formatNumber(value, digits || 0) + (unit || "")
  }
  function time(timestamp) {
    return typeof timestamp === "number" && timestamp > 0
      ? new Date(timestamp * 1000).toLocaleString(Qt.locale(Commons.I18n.formatLocale), "d MMM yyyy, HH:mm:ss")
      : strings.t("time.unknown")
  }
  function state(enabled) { return typeof enabled === "boolean" ? strings.t(enabled ? "device.enabled" : "device.disabled") : unknown }
  function battery(level) { return level === 101 ? strings.t("device.externalPower") : number(level, 0, " %") }
  function uptime(seconds) {
    if (typeof seconds !== "number") return unknown
    return strings.t("device.duration").arg(number(Math.floor(seconds / 86400)))
      .arg(number(Math.floor(seconds / 3600) % 24)).arg(number(Math.floor(seconds / 60) % 60))
  }
  function source(node) {
    return strings.t(node.viaMqtt === true ? "device.ui.sourceMqtt"
      : node.viaMqtt === false ? "device.ui.sourceRadio" : "device.ui.sourceUnknown")
  }
  function radioRows() {
    return [row("model", value(device.hardwareModel)), row("firmware", value(device.firmware)),
      row("preset", value(device.modemPreset)), row("region", value(device.regionName))]
  }
  function powerRows() {
    return [row("battery", battery(device.batteryLevel)), row("voltage", number(device.voltage, 2, " V")),
      row("uptime", uptime(device.uptimeSeconds)), row("channelLoad", number(device.channelUtilization, 2, " %"))]
  }
  function evidenceRows() {
    return [row("lastRx", time(diagnostics.lastRadioRxAt)), row("rxFrom", value(diagnostics.lastRadioRxFrom)),
      row("snr", number(diagnostics.lastRadioRxSnr, 1, " dB")),
      row("rssi", number(diagnostics.lastRadioRxRssi, 0, " dBm")),
      row("receivedPackets", number(diagnostics.receivedRadioPackets)),
      row("heardSinceConnect", number(diagnostics.heardSinceConnect)),
      row("queue", strings.t("device.queueValues").arg(number(diagnostics.queueFree)).arg(number(diagnostics.queueMax))),
      row("routing", value(diagnostics.lastRoutingReason)), row("routingAt", time(diagnostics.lastRoutingAt))]
  }
  function technicalRows() {
    return [row("id", value(device.id)),
      row("transport", device.transport === "usb" ? "USB" : device.transport === "ble" ? "Bluetooth" : unknown),
      row("endpoint", value(device.endpoint || device.port)),
      row("connectedSince", radio.connected ? time(device.connectedSince) : unknown),
      row("updatedAt", time(diagnostics.updatedAt)), row("role", value(device.role)),
      row("frequencySlot", number(device.frequencySlot)), row("frequency", number(device.frequencyMHz, 3, " MHz")),
      row("hopLimit", number(device.hopLimit)), row("txPower", number(device.txPower, 0, " dBm")),
      row("txEnabled", state(device.txEnabled)), row("airUtilTx", number(device.airUtilTx, 2, " %")),
      row("bluetoothEnabled", state(device.bluetoothEnabled))]
  }

  component Rows: ColumnLayout {
    required property var rows
    spacing: Style.spacing.sm
    Repeater {
      model: parent.rows
      delegate: DetailRow {
        required property var modelData
        Layout.fillWidth: true
        label: modelData.label
        value: modelData.value
        fontSize: view.bodyFontSize
      }
    }
  }

  ColumnLayout {
    width: view.availableWidth
    spacing: Style.spacing.xl

    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.sm
      PlainText {
        Layout.fillWidth: true
        text: view.value(view.device.name)
        font.pixelSize: view.titleFontSize
        font.bold: true
        wrapMode: Text.Wrap
      }
      PlainText {
        Layout.fillWidth: true
        text: view.radio.connected
          ? view.strings.t("device.ui.connectionStatus").arg(view.device.transport === "ble" ? "Bluetooth" : view.device.transport === "usb" ? "USB" : view.unknown)
          : view.strings.t("device.disconnected")
        font.pixelSize: view.bodyFontSize
        font.bold: true
        color: view.radio.connected ? Commons.Color.accent : Commons.Color.popups.text
        wrapMode: Text.Wrap
      }
      PlainText {
        Layout.fillWidth: true
        text: view.strings.t(view.radio.connected ? "device.passiveHint" : "device.staleHint")
        font.pixelSize: view.bodyFontSize
        wrapMode: Text.Wrap
      }
    }

    GridLayout {
      uniformCellWidths: true
      Layout.fillWidth: true
      columns: width >= Style.space(700) ? 2 : 1
      columnSpacing: Style.space(32)
      rowSpacing: Style.spacing.xl
      ColumnLayout {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignTop
        spacing: Style.spacing.sm
        Ui.PanelSectionHeader {
          Layout.fillWidth: true
          text: view.strings.t("device.section.radio")
          fontSize: view.sectionFontSize
          foreground: Commons.Color.popups.text
          color: Commons.Color.popups.text
        }
        Rows { Layout.fillWidth: true; rows: view.radioRows() }
      }
      ColumnLayout {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignTop
        spacing: Style.spacing.sm
        Ui.PanelSectionHeader {
          Layout.fillWidth: true
          text: view.strings.t("device.section.power")
          fontSize: view.sectionFontSize
          foreground: Commons.Color.popups.text
          color: Commons.Color.popups.text
        }
        Rows { Layout.fillWidth: true; rows: view.powerRows() }
      }
    }

    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.sm
      Ui.PanelSectionHeader {
        Layout.fillWidth: true
        text: view.strings.t("device.section.evidence")
        fontSize: view.sectionFontSize
        foreground: Commons.Color.popups.text
        color: Commons.Color.popups.text
      }
      PlainText {
        Layout.fillWidth: true
        text: view.label("sessionHint")
        font.pixelSize: view.bodyFontSize
        wrapMode: Text.Wrap
      }
      GridLayout {
        uniformCellWidths: true
        Layout.fillWidth: true
        columns: width >= Style.space(700) ? 2 : 1
        columnSpacing: Style.space(32)
        rowSpacing: Style.spacing.sm
        Repeater {
          model: view.evidenceRows()
          delegate: DetailRow {
            required property var modelData
            Layout.fillWidth: true
            label: modelData.label
            value: modelData.value
          }
        }
      }
    }

    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.sm
      Ui.Button {
        text: view.label(view.technicalExpanded ? "hideTechnical" : "showTechnical")
        fontSize: view.bodyFontSize
        foreground: Commons.Color.popups.text
        bordered: true
        focusable: true
        selected: view.technicalExpanded
        Layout.minimumHeight: 36
        onClicked: view.technicalExpanded = !view.technicalExpanded
      }
      Loader {
        Layout.fillWidth: true
        active: view.technicalExpanded
        visible: active
        sourceComponent: Rows { rows: view.technicalRows() }
      }
    }

    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      Ui.PanelSectionHeader {
        Layout.fillWidth: true
        text: view.strings.t("device.section.peers")
        fontSize: view.sectionFontSize
        foreground: Commons.Color.popups.text
        color: Commons.Color.popups.text
      }
      PlainText {
        Layout.fillWidth: true
        text: view.strings.tn("device.peerRecords", view.radio.nodes.length).arg(view.number(view.radio.nodes.length))
        font.pixelSize: view.bodyFontSize
        wrapMode: Text.Wrap
      }
      PlainText {
        Layout.fillWidth: true
        visible: typeof view.device.nodeCount === "number"
        text: view.strings.tn("device.knownNodes", view.device.nodeCount).arg(view.number(view.device.nodeCount))
        font.pixelSize: view.bodyFontSize
        wrapMode: Text.Wrap
      }
      PlainText {
        Layout.fillWidth: true
        text: view.strings.t("device.peersHint")
        font.pixelSize: view.bodyFontSize
        wrapMode: Text.Wrap
      }
      PlainText {
        Layout.fillWidth: true
        visible: view.radio.nodes.length === 0
        text: view.strings.t("device.peersEmpty")
        font.pixelSize: view.bodyFontSize
        wrapMode: Text.Wrap
      }
      Repeater {
        model: view.radio.nodes
        delegate: ColumnLayout {
          id: peer
          required property var modelData
          readonly property bool expanded: view.selectedPeerId === String(modelData.id)
          Layout.fillWidth: true
          spacing: Style.spacing.sm
          RowLayout {
            Layout.fillWidth: true
            PlainText {
              Layout.fillWidth: true
              text: view.value(peer.modelData.name || peer.modelData.id)
              font.pixelSize: view.bodyFontSize
              font.bold: true
              wrapMode: Text.Wrap
            }
            Ui.Button {
              text: view.label(peer.expanded ? "hidePeer" : "showPeer")
              fontSize: view.bodyFontSize
              foreground: Commons.Color.popups.text
              bordered: true
              focusable: true
              selected: peer.expanded
              Layout.minimumHeight: 36
              onClicked: view.selectedPeerId = peer.expanded ? "" : String(peer.modelData.id)
            }
          }
          GridLayout {
            uniformCellWidths: true
            Layout.fillWidth: true
            columns: width >= Style.space(700) ? 2 : 1
            columnSpacing: Style.space(32)
            DetailRow { Layout.fillWidth: true; label: view.label("lastHeard"); value: view.time(peer.modelData.lastHeard) }
            DetailRow { Layout.fillWidth: true; label: view.label("source"); value: view.source(peer.modelData) }
            DetailRow {
              Layout.fillWidth: true
              label: view.label("signal")
              value: view.strings.t("device.signalValues").arg(view.number(peer.modelData.snr, 1)).arg(view.number(peer.modelData.rssi))
            }
            DetailRow { Layout.fillWidth: true; label: view.label("hops"); value: view.number(peer.modelData.hopsAway) }
          }
          Loader {
            Layout.fillWidth: true
            active: peer.expanded
            visible: active
            sourceComponent: Rows {
              rows: [view.row("id", view.value(peer.modelData.id)),
                view.row("model", view.value(peer.modelData.hardwareModel)),
                view.row("role", view.value(peer.modelData.role)),
                view.row("battery", view.battery(peer.modelData.batteryLevel)),
                view.row("voltage", view.number(peer.modelData.voltage, 2, " V")),
                view.row("latitude", view.number(peer.modelData.latitude, 5)),
                view.row("longitude", view.number(peer.modelData.longitude, 5))]
            }
          }
          Rectangle {
            Layout.fillWidth: true
            implicitHeight: 1
            color: Commons.Color.popups.text
            opacity: 0.15
          }
        }
      }
    }
  }
}
