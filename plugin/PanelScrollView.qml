import QtQuick
import QtQuick.Controls as Controls
import qs.Commons

Controls.ScrollView {
  id: view
  readonly property real bodyFontSize: Math.max(14, Style.font.body)
  readonly property real sectionFontSize: Math.max(16, Style.font.body)
  readonly property real titleFontSize: Math.max(20, Style.font.heading)
  clip: true
  contentWidth: availableWidth
  activeFocusOnTab: true
  Controls.ScrollBar.horizontal.policy: Controls.ScrollBar.AlwaysOff

  Keys.onPressed: function(event) {
    var flick = view.contentItem
    var target = flick.contentY
    if (event.key === Qt.Key_PageDown) target += flick.height * 0.85
    else if (event.key === Qt.Key_PageUp) target -= flick.height * 0.85
    else if (event.key === Qt.Key_Down) target += Style.space(32)
    else if (event.key === Qt.Key_Up) target -= Style.space(32)
    else if (event.key === Qt.Key_Home) target = flick.originY
    else if (event.key === Qt.Key_End) target = flick.originY + flick.contentHeight - flick.height
    else { event.accepted = false; return }
    flick.contentY = Math.max(flick.originY, Math.min(flick.originY + Math.max(0, flick.contentHeight - flick.height), target))
    event.accepted = true
  }
}
