import QtQuick
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui as Ui
import "PreviewModel.js" as PreviewModel

PanelScrollView {
  id: view
  required property var radio
  required property var strings
  required property var controller
  property bool helpExpanded: false
  readonly property color foreground: Commons.Color.popups.text
  readonly property var current: radio.device || ({})
  readonly property var preview: controller.preview
  readonly property bool inputIsUrl: linkInput.text.trim().indexOf("://") >= 0
  readonly property string operation: preview ? String(preview.operation || "replace") : "replace"
  readonly property string targetName: preview ? String(preview.targetName || "").slice(0, 80) : ""
  readonly property bool keyNameVisible: !inputIsUrl && (linkInput.text !== "" || !preview || operation !== "replace")

  function operationText(replaceKey, addKey, updateKey) {
    if (operation === "add") return strings.t(addKey).arg(targetName)
    if (operation === "update") return strings.t(updateKey).arg(targetName)
    return strings.t(replaceKey)
  }

  function clearSecret() { linkInput.text = "" }
  function valueText(value) {
    return value === undefined || value === null || value === "" ? strings.t("device.unknown") : String(value)
  }
  function slotText(value) {
    return typeof value === "number" && isFinite(value) ? Commons.I18n.formatNumber(value) : strings.t("device.unknown")
  }
  function txText(value) {
    return strings.t(typeof value !== "boolean" ? "device.unknown" : value ? "device.enabled" : "device.disabled")
  }

  component Body: Text {
    Layout.fillWidth: true
    textFormat: Text.PlainText
    font.family: Style.font.family
    font.pixelSize: view.bodyFontSize
    color: view.foreground
    wrapMode: Text.Wrap
  }
  component Action: Ui.Button {
    fontSize: view.bodyFontSize
    foreground: view.foreground
    focusable: true
    Layout.minimumHeight: Style.space(36)
    opacity: enabled ? 1 : 0.5
  }

  ColumnLayout {
    width: view.availableWidth
    spacing: Style.spacing.lg

    Body {
      text: view.strings.t("settings.ui.current")
      font.pixelSize: view.titleFontSize
      font.bold: true
    }
    GridLayout {
      uniformCellWidths: true
      Layout.fillWidth: true
      columns: width >= Style.space(700) ? 2 : 1
      columnSpacing: Style.space(32)
      rowSpacing: Style.spacing.xs
      DetailRow { Layout.fillWidth: true; label: view.strings.t("settings.ui.region"); value: view.valueText(view.current.regionName) }
      DetailRow { Layout.fillWidth: true; label: view.strings.t("settings.ui.preset"); value: view.valueText(view.current.modemPreset) }
      DetailRow { Layout.fillWidth: true; label: view.strings.t("settings.ui.slot"); value: view.slotText(view.current.frequencySlot) }
      DetailRow { Layout.fillWidth: true; label: view.strings.t("settings.ui.transmit"); value: view.txText(view.current.txEnabled) }
    }
    DetailRow {
      Layout.fillWidth: true
      label: view.strings.t("settings.ui.activeChannels")
      value: view.radio.channels.length ? view.radio.channels.map(function(channel) { return channel.name }).join(" · ") : view.strings.t("device.unknown")
    }
    Body { text: view.strings.t("settings.ui.sharedRadio") }

    GridLayout {
      uniformCellWidths: true
      Layout.fillWidth: true
      columns: width >= Style.space(700) ? 2 : 1
      columnSpacing: Style.space(32)
      rowSpacing: Style.spacing.xl

      ColumnLayout {
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        Layout.alignment: Qt.AlignTop
        spacing: Style.spacing.md
        Ui.PanelSectionHeader { Layout.fillWidth: true; text: view.strings.t("settings.ui.berlin"); fontSize: view.sectionFontSize; foreground: view.foreground; color: view.foreground }
        Body { text: "MediumFast + TEST"; font.bold: true }
        Body { text: view.strings.t("settings.ui.starterHint") }
        Action {
          objectName: "meshtasticBerlinPreview"
          bordered: true
          text: view.strings.t("settings.ui.previewStarter")
          enabled: view.radio.available && !view.controller.busyConfig
          onClicked: view.controller.previewConfig(true, "", "")
        }
        Action {
          text: view.strings.t("settings.ui.source")
          onClicked: Quickshell.execDetached(["xdg-open", "https://potatomesh.net/pages/about"])
        }
      }

      ColumnLayout {
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        Layout.alignment: Qt.AlignTop
        spacing: Style.spacing.md
        Ui.PanelSectionHeader { Layout.fillWidth: true; text: view.strings.t("settings.importTitle"); fontSize: view.sectionFontSize; foreground: view.foreground; color: view.foreground }
        Body { text: view.strings.t("settings.ui.valueLabel"); font.bold: true }
        Ui.TextField {
          id: linkInput
          Layout.fillWidth: true
          Layout.minimumHeight: Style.space(40)
          font.pixelSize: view.bodyFontSize
          password: true
          maximumLength: 16384
          placeholderText: view.strings.t("settings.valuePlaceholder")
          foreground: view.foreground
          placeholderTextColor: Qt.darker(view.foreground, 1.2)
          enabled: !view.controller.busyConfig
          onTextEdited: view.controller.resetConfigPreview()
        }
        Body { text: view.strings.t("settings.ui.privateInput") }
        ColumnLayout {
          Layout.fillWidth: true
          visible: view.keyNameVisible
          spacing: Style.spacing.xs
          Body { text: view.strings.t("settings.ui.channelName"); font.bold: true }
          Ui.TextField {
            id: channelNameInput
            objectName: "meshtasticChannelName"
            Layout.fillWidth: true
            Layout.minimumHeight: Style.space(40)
            font.pixelSize: view.bodyFontSize
            text: "BerlinMesh"
            foreground: view.foreground
            enabled: !view.controller.busyConfig
            onTextEdited: view.controller.resetConfigPreview()
          }
          Body { text: view.strings.t("settings.ui.channelNameHint") }
        }
        Action {
          objectName: "meshtasticUrlPreview"
          text: view.strings.t("settings.preview")
          bordered: true
          enabled: view.radio.available && linkInput.text.trim() !== "" && !view.controller.busyConfig
          onClicked: view.controller.previewConfig(false, linkInput.text, view.inputIsUrl ? "" : channelNameInput.text)
        }
      }
    }

    Body { visible: view.controller.busyConfig; text: view.strings.t("settings.working"); font.bold: true }
    Body {
      visible: view.controller.configError !== "" || !view.radio.available
      text: view.controller.errorText(view.controller.configError || "backend_unavailable")
      color: Commons.Color.urgent
    }
    Body { visible: view.controller.configAccepted; text: view.strings.t("settings.accepted"); font.bold: true }

    ColumnLayout {
      Layout.fillWidth: true
      visible: view.preview !== null
      spacing: Style.spacing.md
      Ui.PanelSectionHeader { Layout.fillWidth: true; text: view.strings.t("settings.ui.previewTitle"); fontSize: view.sectionFontSize; foreground: view.foreground; color: view.foreground }
      Body {
        text: view.operationText("settings.ui.previewOnly", "settings.ui.previewAdd", "settings.ui.previewUpdate")
        font.bold: true
      }
      GridLayout {
        uniformCellWidths: true
        Layout.fillWidth: true
        columns: width >= Style.space(700) ? 2 : 1
        columnSpacing: Style.space(32)
        rowSpacing: Style.spacing.xs
        DetailRow { Layout.fillWidth: true; label: view.strings.t("settings.ui.region"); value: view.valueText(view.preview ? view.preview.regionName : null) }
        DetailRow { Layout.fillWidth: true; label: view.strings.t("settings.ui.preset"); value: view.valueText(view.preview ? view.preview.modemPreset : null) }
        DetailRow { Layout.fillWidth: true; label: view.strings.t("settings.ui.slot"); value: view.slotText(view.preview ? view.preview.frequencySlot : null) }
        DetailRow { Layout.fillWidth: true; label: view.strings.t("settings.ui.transmit"); value: view.txText(view.preview ? view.preview.txEnabled : null) }
      }
      Repeater {
        model: view.preview ? PreviewModel.boundedRows(view.preview.channels, 8) : []
        delegate: ColumnLayout {
          required property var modelData
          Layout.fillWidth: true
          spacing: Style.spacing.xs
          DetailRow {
            Layout.fillWidth: true
            label: view.strings.t(modelData.primary ? "settings.ui.primaryChannel" : "settings.ui.channel").arg(Commons.I18n.formatNumber(modelData.index))
            value: String(modelData.name || "").slice(0, 80) || view.strings.t("device.unknown")
          }
          Body { text: view.strings.t(modelData.encrypted ? "settings.ui.encrypted" : "settings.ui.unencrypted") }
        }
      }
      Repeater {
        model: view.preview ? PreviewModel.boundedRows(view.preview.warnings, 16) : []
        delegate: Body {
          required property var modelData
          text: view.strings.t("warning." + (["replaces_channels", "region_preserved", "region_unset", "uplink_enabled", "downlink_enabled", "public_key", "offline_preview", "other_channels_preserved", "berlin_profile_mismatch"].indexOf(modelData) >= 0 ? modelData : "unknown"))
          font.bold: true
        }
      }
      Body {
        visible: !view.radio.connected
        text: view.strings.t("settings.ui.reconnect")
      }
      Action {
        visible: !view.controller.confirming
        text: view.operationText("settings.replace", "settings.add", "settings.update")
        enabled: view.radio.connected && !view.controller.busyConfig
        onClicked: view.controller.confirming = true
      }
      Body {
        visible: view.controller.confirming
        text: view.operationText("settings.confirmHint", "settings.confirmAddHint", "settings.confirmUpdateHint")
        color: Commons.Color.urgent
        font.bold: true
      }
      Flow {
        Layout.fillWidth: true
        spacing: Style.spacing.md
        visible: view.controller.confirming
        Action {
          objectName: "meshtasticApplyConfig"
          text: view.operationText("settings.confirm", "settings.confirmAdd", "settings.confirmUpdate")
          enabled: view.radio.connected && !view.controller.busyConfig
          onClicked: view.controller.applyConfig()
        }
        Action {
          text: view.strings.t("settings.cancel")
          enabled: !view.controller.busyConfig
          onClicked: view.controller.confirming = false
        }
      }
    }

    Action {
      text: view.strings.t(view.helpExpanded ? "settings.ui.hideHelp" : "settings.ui.showHelp")
      selected: view.helpExpanded
      bordered: true
      onClicked: view.helpExpanded = !view.helpExpanded
    }
    ColumnLayout {
      Layout.fillWidth: true
      visible: view.helpExpanded
      spacing: Style.spacing.md
      Ui.PanelSectionHeader { Layout.fillWidth: true; text: view.strings.t("settings.ui.helpTitle"); fontSize: view.sectionFontSize; foreground: view.foreground; color: view.foreground }
      Body { text: view.strings.t("settings.radioHint") }
      Body { text: view.strings.t("settings.berlinHint") }
      Body { text: view.strings.t("settings.importHint") }
    }
  }
}
