import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui as Ui

Ui.BarWidget {
  id: root
  moduleName: "miszarchy.meshtastic"
  LayoutMirroring.enabled: Commons.I18n.rightToLeft
  LayoutMirroring.childrenInherit: true
  property bool opened: false
  readonly property bool popoutSwitchClosing: panel.popoutSwitchClosing
  readonly property int unread: radioService.channels.reduce(function(total, channel) { return total + channel.unread }, 0)

  function open() { opened = true }
  function close() { opened = false }
  function togglePanel() { opened = !opened }
  function closeForPopoutSwitch() { panel.popoutSwitchClosing = true; close() }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Commons.Translations { id: catalog; directory: Qt.resolvedUrl("i18n") }
  RadioClient { id: radioService }

  Ui.BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰍡"
    tooltipText: "Meshtastic"
    active: root.opened || root.unread > 0
    onPressed: function(b) { if (b === Qt.LeftButton) root.togglePanel() }

    Rectangle {
      visible: root.unread > 0
      width: Style.space(5)
      height: width
      radius: width / 2
      color: root.bar ? root.bar.foreground : Commons.Color.foreground
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.margins: Style.space(3)
    }
  }

  MessengerPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    radio: radioService
    strings: catalog
  }
}
