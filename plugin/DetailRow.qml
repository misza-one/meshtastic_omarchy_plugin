import QtQuick
import QtQuick.Layouts
import qs.Commons

RowLayout {
  id: row
  required property string label
  property string value: ""
  property bool muted: false
  property real fontSize: Math.max(14, Style.font.body)
  spacing: Style.space(16)
  Layout.minimumHeight: Style.space(28)

  PlainText {
    Layout.fillWidth: true
    Layout.minimumWidth: 0
    Layout.preferredWidth: 44
    Layout.alignment: Qt.AlignTop
    text: row.label
    font.pixelSize: row.fontSize
    opacity: 0.78
    wrapMode: Text.WordWrap
  }
  PlainText {
    Layout.fillWidth: true
    Layout.minimumWidth: 0
    Layout.preferredWidth: 56
    Layout.alignment: Qt.AlignTop
    text: row.value
    font.pixelSize: row.fontSize
    font.bold: true
    opacity: row.muted ? 0.78 : 1
    horizontalAlignment: Text.AlignRight
    wrapMode: Text.WrapAnywhere
  }
}
