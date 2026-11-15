import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui as Ui

Ui.KeyboardPanel {
  id: panel
  required property var radio
  required property var strings
  property int selectedChannel: -1
  property string displayedIdentity: ""
  property string activeTab: "messages"
  property var scrollPositions: ({})
  property string previewRequest: ""
  property string applyRequest: ""
  property var preview: null
  property string configError: ""
  property bool configAccepted: false
  property bool confirming: false
  readonly property color foreground: Commons.Color.popups.text
  readonly property bool busyConfig: previewRequest !== "" || applyRequest !== ""
  readonly property var channel: radio.channels.find(function(c) { return c.index === panel.selectedChannel }) || null
  readonly property bool canSend: radio.available && radio.connected && channel !== null && channel.identity !== ""
                                  && Number(radio.device.region) !== 0 && radio.device.txEnabled !== false && !radio.pending && !radio.sending
                                  && composer.text.trim().length > 0 && radio.utf8Bytes(composer.text) <= 228
  readonly property string blockedReason: !radio.available ? "backend_unavailable"
    : !radio.connected ? "radio_disconnected" : Number(radio.device.region) === 0 ? "region_unset"
    : radio.device.txEnabled === false ? "tx_disabled"
    : channel === null ? "channel_unavailable" : ""

  centerOnBar: true
  contentWidth: fittedContentWidth(Style.space(activeTab === "messages" ? 760 : 900))
  contentHeight: cappedContentHeight(Style.space(activeTab === "messages" ? 560 : 700))
  focusTarget: activeTab === "settings" ? settingsBody : activeTab === "device" ? deviceBody : composer

  function errorText(code) {
    var known = ["backend_unavailable", "radio_unavailable", "radio_disconnected", "radio_ambiguous",
      "region_unset", "tx_disabled", "channel_unavailable", "invalid_command", "invalid_text", "message_too_long",
      "send_failed", "ack_timeout", "backend_busy", "storage_error", "routing_failed", "send_interrupted",
      "invalid_url", "invalid_config", "invalid_key", "invalid_channel_name", "channels_full", "channel_ambiguous",
      "preview_expired", "confirmation_required", "config_failed",
      "ble_unavailable", "ble_not_found", "ble_pairing_required", "ble_ambiguous", "transport_invalid"]
    return strings.t("error." + (known.indexOf(code) >= 0 ? code : "unknown"))
  }

  function statusText(status) {
    return strings.t("status." + (["queued", "acknowledged", "failed", "unconfirmed", "received"].indexOf(status) >= 0 ? status : "unknown"))
  }

  function messageTime(timestamp) {
    if (!(timestamp > 0) || timestamp > 8640000000000) return strings.t("time.unknown")
    return new Date(timestamp * 1000).toLocaleString(Qt.locale(Commons.I18n.formatLocale), "d MMM, HH:mm")
  }

  function markRead() {
    if (open && activeTab === "messages" && channel && channel.unread > 0)
      radio.command({action: "read", channel: selectedChannel})
  }

  function captureScroll() {
    var index = timeline.indexAt(1, timeline.contentY + 1)
    var item = index >= 0 ? timeline.itemAtIndex(index) : null
    return {bottom: timeline.atYEnd || timeline.count === 0,
            id: index >= 0 && index < messageModel.count ? messageModel.get(index).messageId : "",
            offset: item ? item.y - timeline.contentY : 0}
  }

  function restoreScroll(position) {
    if (!position || position.bottom) { timeline.positionViewAtEnd(); return }
    for (var i = 0; i < messageModel.count; i++) {
      if (messageModel.get(i).messageId !== position.id) continue
      timeline.positionViewAtIndex(i, ListView.Beginning)
      var item = timeline.itemAtIndex(i)
      if (item) timeline.contentY = item.y - position.offset
      return
    }
  }

  function selectChannel(index) {
    if (index === selectedChannel) return
    if (selectedChannel >= 0) {
      var positions = Object.assign({}, scrollPositions)
      positions[displayedIdentity] = captureScroll()
      scrollPositions = positions
    }
    selectedChannel = index
    displayedIdentity = radio.channelIdentity(index)
    composer.text = radio.draft(index)
    syncMessages(true)
    markRead()
  }

  function syncMessages(switched) {
    var incoming = radio.messages.filter(function(m) { return m.channel === panel.selectedChannel })
    var position = switched ? scrollPositions[displayedIdentity] : captureScroll()
    var removed = false
    if (switched) messageModel.clear()
    // Reconcile by persistent id: telemetry snapshots must not reset the view.
    for (var i = 0; i < incoming.length; i++) {
      var message = incoming[i]
      if (i < messageModel.count && messageModel.get(i).messageId !== message.messageId) {
        var match = -1
        for (var j = i + 1; j < messageModel.count; j++) {
          if (messageModel.get(j).messageId === message.messageId) { match = j; break }
        }
        if (match >= 0) { messageModel.remove(i, match - i); removed = true }
        else messageModel.insert(i, message)
      }
      if (i >= messageModel.count) messageModel.append(message)
      else {
        var current = messageModel.get(i)
        for (var key in message) if (current[key] !== message[key]) messageModel.setProperty(i, key, message[key])
      }
    }
    if (messageModel.count > incoming.length) {
      messageModel.remove(incoming.length, messageModel.count - incoming.length)
      removed = true
    }
    if (switched || removed || !position || position.bottom)
      Qt.callLater(function() { panel.restoreScroll(position) })
  }

  function updateState() {
    if (!channel && radio.channels.length > 0) selectChannel(radio.channels[0].index)
    else {
      var identity = radio.channelIdentity(selectedChannel)
      if (displayedIdentity !== identity) {
        if (displayedIdentity) {
          var positions = Object.assign({}, scrollPositions)
          positions[displayedIdentity] = captureScroll()
          scrollPositions = positions
        }
        displayedIdentity = identity
        composer.text = radio.draft(selectedChannel)
        syncMessages(true)
      } else syncMessages(false)
    }
    markRead()
  }

  function resetConfigPreview() {
    configError = ""
    preview = null
    confirming = false
    configAccepted = false
  }

  function previewConfig(berlin, value, channelName) {
    resetConfigPreview()
    previewRequest = radio.request(berlin ? "preview_berlin" : "preview_config", berlin ? {} : {value: value, channelName: channelName})
    if (!previewRequest) configError = "backend_unavailable"
  }

  function applyConfig() {
    if (!preview || !confirming) return
    configError = ""
    applyRequest = radio.request("apply_config", {token: preview.token, confirmed: true})
    if (!applyRequest) configError = "backend_unavailable"
    confirming = false
  }

  onOpenChanged: if (open) markRead()
  onActiveTabChanged: {
    markRead()
    Qt.callLater(function() { if (panel.open) panel.focusTarget.forceActiveFocus() })
  }

  readonly property Connections radioEvents: Connections {
    target: panel.radio
    function onSnapshot() {
      panel.updateState()
      if (!panel.radio.available && panel.busyConfig) {
        panel.previewRequest = ""
        panel.applyRequest = ""
        panel.configError = "backend_unavailable"
      }
    }
    function onDraftsChanged() { composer.text = panel.radio.draft(panel.selectedChannel) }
    function onSent(channel) { if (channel === panel.selectedChannel) Qt.callLater(function() { timeline.positionViewAtEnd() }) }
    function onResult(response) {
      if (response.requestId === panel.previewRequest && panel.previewRequest !== "") {
        panel.previewRequest = ""
        if (response.ok && response.preview) {
          panel.preview = response.preview
          settingsBody.clearSecret()
        } else panel.configError = response.error || "invalid_config"
      } else if (response.requestId === panel.applyRequest && panel.applyRequest !== "") {
        panel.applyRequest = ""
        if (response.ok) {
          panel.configAccepted = true
          panel.preview = null
        } else panel.configError = response.error || "config_failed"
      }
    }
  }

  readonly property ListModel messageStore: ListModel { id: messageModel }

  Item {
    anchors.fill: parent
    Keys.onEscapePressed: function(event) { panel.close(); event.accepted = true }
    Keys.onPressed: function(event) {
      if ((event.modifiers & Qt.ControlModifier) && (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab)) {
        var tabs = ["messages", "settings", "device"]
        var reverse = event.key === Qt.Key_Backtab || (event.modifiers & Qt.ShiftModifier)
        panel.activeTab = tabs[(tabs.indexOf(panel.activeTab) + (reverse ? 2 : 1)) % tabs.length]
        event.accepted = true
      }
    }

    ColumnLayout {
      anchors.fill: parent
      spacing: Style.spacing.lg

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xs
          PlainText {
            Layout.fillWidth: true
            text: "Meshtastic"
            font.pixelSize: Style.font.heading
            font.bold: true
          }
          PlainText {
            Layout.fillWidth: true
            text: panel.radio.connected
              ? panel.strings.t("header.connected").arg(panel.radio.device.name || panel.radio.device.id)
              : panel.strings.t("header.offline")
            elide: Text.ElideRight
          }
        }
        PlainText {
          visible: panel.radio.device.transport === "usb" || panel.radio.device.transport === "ble"
          text: panel.radio.device.transport === "ble" ? "Bluetooth" : "USB"
          font.pixelSize: Style.font.bodySmall
        }
        Ui.Button {
          text: panel.radio.notificationsEnabled ? panel.strings.t("notifications.on") : panel.strings.t("notifications.off")
          tooltipText: panel.strings.t("notifications.hint")
          foreground: panel.foreground
          focusable: true
          enabled: panel.radio.available
          opacity: enabled ? 1 : 0.5
          onClicked: panel.radio.command({action: "notifications", enabled: !panel.radio.notificationsEnabled})
        }
        Ui.Button {
          iconText: "󰅖"
          tooltipText: panel.strings.t("close")
          foreground: panel.foreground
          focusable: true
          onClicked: panel.close()
        }
      }

      PlainText {
        Layout.fillWidth: true
        visible: panel.radio.device.firmware !== undefined && panel.radio.device.firmware !== ""
        text: (typeof panel.radio.device.nodeCount === "number"
          ? panel.strings.tn("header.details", panel.radio.device.nodeCount)
            .arg(panel.radio.device.firmware || "—").arg(panel.radio.device.regionName || "—")
            .arg(Commons.I18n.formatNumber(panel.radio.device.nodeCount))
          : panel.strings.t("header.detailsUnknown").arg(panel.radio.device.firmware || "—")
            .arg(panel.radio.device.regionName || "—"))
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.Wrap
      }

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.sm
        Ui.Button {
          text: panel.strings.t("tab.messages")
          selected: panel.activeTab === "messages"
          foreground: panel.foreground
          focusable: true
          onClicked: panel.activeTab = "messages"
        }
        Ui.Button {
          text: panel.strings.t("tab.settings")
          objectName: "meshtasticSettingsTab"
          selected: panel.activeTab === "settings"
          foreground: panel.foreground
          focusable: true
          onClicked: panel.activeTab = "settings"
        }
        Ui.Button {
          text: panel.strings.t("tab.device")
          objectName: "meshtasticDeviceTab"
          selected: panel.activeTab === "device"
          foreground: panel.foreground
          focusable: true
          onClicked: panel.activeTab = "device"
        }
        Item { Layout.fillWidth: true }
      }

      PlainText {
        Layout.fillWidth: true
        visible: panel.radio.error !== "" || (panel.activeTab === "messages" && (panel.radio.actionError !== "" || panel.blockedReason !== ""))
        text: panel.errorText((panel.activeTab === "messages" ? panel.radio.actionError : "") || panel.radio.error || panel.blockedReason)
        color: Commons.Color.urgent
        wrapMode: Text.Wrap
      }

      RowLayout {
        visible: panel.activeTab === "messages"
        Layout.fillWidth: true
        Layout.fillHeight: true
        spacing: Style.spacing.xxl

        ColumnLayout {
          Layout.preferredWidth: Math.min(Style.space(180), panel.contentWidth * 0.28)
          Layout.fillHeight: true
          spacing: Style.spacing.lg
          PlainText { text: panel.strings.t("channels.title"); font.bold: true }
          ListView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: Style.spacing.sm
            model: panel.radio.channels
            boundsBehavior: Flickable.StopAtBounds
            Controls.ScrollBar.vertical: Controls.ScrollBar { policy: Controls.ScrollBar.AsNeeded }
            delegate: Ui.Button {
              required property var modelData
              width: ListView.view.width
              height: Style.space(58)
              selected: panel.selectedChannel === modelData.index
              foreground: panel.foreground
              focusable: true
              onClicked: { panel.selectChannel(modelData.index); composer.forceActiveFocus() }
              Column {
                anchors.fill: parent
                anchors.margins: Style.spacing.lg
                spacing: Style.spacing.xs
                PlainText {
                  width: parent.width
                  text: modelData.unread > 0
                    ? panel.strings.t("channels.unreadName").arg(modelData.name).arg(Commons.I18n.formatNumber(modelData.unread))
                    : modelData.name
                  font.bold: panel.selectedChannel === modelData.index || modelData.unread > 0
                  elide: Text.ElideRight
                }
                PlainText {
                  width: parent.width
                  text: panel.strings.t(modelData.encrypted ? "channels.encrypted" : "channels.public").arg(Commons.I18n.formatNumber(modelData.index))
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }
              }
            }
          }
          PlainText {
            Layout.fillWidth: true
            visible: panel.radio.channels.length === 0
            text: panel.strings.t("channels.empty")
            wrapMode: Text.Wrap
          }
        }

        Rectangle { Layout.fillHeight: true; width: 1; color: Style.normalBorderFor(panel.foreground, Commons.Color.accent) }

        ColumnLayout {
          Layout.fillWidth: true
          Layout.fillHeight: true
          spacing: Style.spacing.lg
          PlainText {
            Layout.fillWidth: true
            text: panel.channel ? panel.channel.name : panel.strings.t("channels.choose")
            font.bold: true
            elide: Text.ElideRight
          }
          Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            ListView {
              id: timeline
              anchors.fill: parent
              clip: true
              spacing: Style.spacing.lg
              model: messageModel
              boundsBehavior: Flickable.StopAtBounds
              Controls.ScrollBar.vertical: Controls.ScrollBar { policy: Controls.ScrollBar.AsNeeded }
              delegate: Item {
                id: messageItem
                required property string messageId
                required property string sender
                required property string body
                required property real timestamp
                required property bool outgoing
                required property string status
                required property string failure
                width: timeline.width
                height: messageContent.implicitHeight + Style.spacing.lg * 2
                Rectangle {
                  width: messageContent.width + Style.spacing.lg * 2
                  height: parent.height
                  x: messageContent.x - Style.spacing.lg
                  color: messageItem.outgoing ? Style.normalFillFor(panel.foreground, Commons.Color.accent) : "transparent"
                  radius: Style.cornerRadius
                }
                ColumnLayout {
                  id: messageContent
                  width: Math.max(1, parent.width * 0.9 - Style.spacing.lg * 2)
                  x: messageItem.outgoing !== Commons.I18n.rightToLeft
                    ? parent.width - width - Style.spacing.lg : Style.spacing.lg
                  y: Style.spacing.lg
                  spacing: Style.spacing.sm
                  RowLayout {
                    Layout.fillWidth: true
                    PlainText {
                      Layout.fillWidth: true
                      text: messageItem.outgoing ? panel.strings.t("message.you") : messageItem.sender
                      font.bold: true
                      font.pixelSize: Style.font.bodySmall
                      elide: Text.ElideRight
                    }
                    PlainText {
                      text: panel.messageTime(messageItem.timestamp)
                      font.pixelSize: Style.font.caption
                    }
                  }
                  PlainText {
                    Layout.fillWidth: true
                    text: messageItem.body
                    wrapMode: Text.Wrap
                  }
                  PlainText {
                    Layout.fillWidth: true
                    visible: messageItem.outgoing
                    text: messageItem.failure !== "" ? panel.errorText(messageItem.failure) : panel.statusText(messageItem.status)
                    font.pixelSize: Style.font.caption
                    color: messageItem.status === "failed" ? Commons.Color.urgent : panel.foreground
                    wrapMode: Text.Wrap
                  }
                }
              }
            }
            Column {
              anchors.centerIn: parent
              width: Math.max(1, parent.width - Style.space(24))
              visible: messageModel.count === 0
              spacing: Style.spacing.lg
              PlainText {
                width: parent.width
                text: panel.strings.t("timeline.empty")
                font.bold: true
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
              }
              PlainText {
                width: parent.width
                text: panel.strings.t(panel.radio.connected ? "timeline.listening" : "timeline.offline")
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
              }
            }
          }

          Controls.ScrollView {
            Layout.fillWidth: true
            Layout.preferredHeight: Style.space(76)
            clip: true
            Controls.TextArea {
              id: composer
              objectName: "meshtasticComposer"
              textFormat: TextEdit.PlainText
              placeholderText: panel.strings.t("compose.placeholder")
              wrapMode: TextEdit.Wrap
              selectByMouse: true
              readOnly: panel.selectedChannel < 0 || (panel.radio.pending !== null && panel.radio.pending.channel === panel.selectedChannel)
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              color: panel.foreground
              placeholderTextColor: panel.foreground
              selectionColor: Style.selectionFillFor(panel.foreground, Commons.Color.accent)
              selectedTextColor: panel.foreground
              padding: Style.spacing.lg
              background: Ui.BorderSurface {
                color: Style.controlFill(composer.activeFocus, composer.hovered, panel.foreground, Commons.Color.accent)
                borderSpec: Border.controlSpec(composer.activeFocus ? "focus" : "normal", panel.foreground, Commons.Color.accent)
                radius: Style.cornerRadius
              }
              onTextChanged: panel.radio.setDraft(panel.selectedChannel, text)
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Escape) { panel.close(); event.accepted = true }
                else if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && !(event.modifiers & Qt.ShiftModifier)) {
                  if (panel.canSend) panel.radio.send(panel.selectedChannel)
                  event.accepted = true
                }
              }
            }
          }
          RowLayout {
            Layout.fillWidth: true
            PlainText {
              Layout.fillWidth: true
              text: panel.strings.t("compose.hint")
              font.pixelSize: Style.font.caption
              wrapMode: Text.Wrap
            }
            PlainText {
              text: panel.strings.t("compose.bytes").arg(Commons.I18n.formatNumber(panel.radio.utf8Bytes(composer.text)))
                .arg(Commons.I18n.formatNumber(228))
              font.pixelSize: Style.font.bodySmall
              color: panel.radio.utf8Bytes(composer.text) > 228 ? Commons.Color.urgent : panel.foreground
            }
            Ui.Button {
              text: panel.radio.pending || panel.radio.sending ? panel.strings.t("compose.sending") : panel.strings.t("compose.send")
              foreground: panel.foreground
              enabled: panel.canSend
              opacity: enabled ? 1 : 0.5
              focusable: true
              onClicked: panel.radio.send(panel.selectedChannel)
            }
          }
        }
      }

      DevicePanel {
        id: deviceBody
        objectName: "meshtasticDevicePanel"
        visible: panel.activeTab === "device"
        Layout.fillWidth: true
        Layout.fillHeight: true
        radio: panel.radio
        strings: panel.strings
      }

      SettingsPanel {
        id: settingsBody
        objectName: "meshtasticSettingsPanel"
        visible: panel.activeTab === "settings"
        Layout.fillWidth: true
        Layout.fillHeight: true
        radio: panel.radio
        strings: panel.strings
        controller: panel
      }
    }
  }
}
