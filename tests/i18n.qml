import QtQuick
import Quickshell
import "./Commons" as Commons

ShellRoot {
  id: root
  property int stage: 0
  property int count: 0
  readonly property var languages: ["en", "pl", "de", "de-AT", "pl-PL", "fr", "ar", "fa", "en"]
  readonly property var counts: [0, 1, 2, 4, 5, 11, 12, 14, 21, 22, 24, 112]
  property Commons.Translations strings: Commons.Translations { directory: Qt.resolvedUrl("i18n") }
  property Text records: Text {
    textFormat: Text.PlainText
    text: root.strings.tn("device.peerRecords", root.count).arg(Commons.I18n.formatNumber(root.count))
  }
  property Text header: Text {
    textFormat: Text.PlainText
    text: root.strings.tn("header.details", root.count).arg("2.7.11").arg("EU_868")
      .arg(Commons.I18n.formatNumber(root.count))
  }
  property Text nodes: Text {
    textFormat: Text.PlainText
    text: root.strings.tn("device.knownNodes", root.count).arg(Commons.I18n.formatNumber(root.count))
  }
  property Text action: Text { text: root.strings.t("notifications.open") }
  property Text number: Text { text: Commons.I18n.formatNumber(-1234.5, 2) }
  property Text bytes: Text {
    text: root.strings.t("compose.bytes").arg(Commons.I18n.formatNumber(12)).arg(Commons.I18n.formatNumber(228))
  }

  function check(condition, message) {
    if (!condition) throw new Error(languages[stage] + ": " + message)
  }

  function localizedDigits(value, language) {
    if (language !== "fa") return String(value)
    return String(value).replace(/[0-9]/g, function(digit) { return "۰۱۲۳۴۵۶۷۸۹"[Number(digit)] })
  }

  function verify() {
    var language = languages[stage]
    var base = language.split("-")[0]
    var polish = base === "pl"
    var german = base === "de"
    var forms = ["many", "one", "few", "few", "many", "many", "many", "many", "many", "few", "few", "many"]
    for (var i = 0; i < counts.length; i++) {
      count = counts[i]
      var n = localizedDigits(count, language)
      var expectedRecords
      var expectedHeader
      var expectedNodes
      if (polish) {
        var suffix = forms[i] === "one" ? "wpis węzła." : forms[i] === "few" ? "wpisy węzłów." : "wpisów węzłów."
        var nodeSuffix = forms[i] === "one" ? "węzeł" : forms[i] === "few" ? "węzły" : "węzłów"
        var headerSuffix = forms[i] === "one" ? "znany węzeł" : forms[i] === "few" ? "znane węzły" : "znanych węzłów"
        expectedRecords = "Pokazano " + n + " " + suffix
        expectedHeader = "Oprogramowanie 2.7.11 · EU_868 · " + n + " " + headerSuffix
        expectedNodes = "Radio zna " + n + " " + nodeSuffix + "."
      } else if (german) {
        expectedRecords = n + (count === 1 ? " Knoteneintrag wird angezeigt." : " Knoteneinträge werden angezeigt.")
        expectedHeader = "Firmware 2.7.11 · EU_868 · " + n + (count === 1 ? " bekannter Knoten" : " bekannte Knoten")
        expectedNodes = "Das Funkgerät kennt " + n + " Knoten."
      } else {
        // French/Persian zero and Arabic two must use the fallback's EN rules.
        expectedRecords = "Showing " + n + (count === 1 ? " peer record." : " peer records.")
        expectedHeader = "Firmware 2.7.11 · EU_868 · " + n + (count === 1 ? " known node" : " known nodes")
        expectedNodes = "The radio knows " + n + (count === 1 ? " node." : " nodes.")
      }
      check(records.text === expectedRecords, "peer count " + count + ": " + records.text)
      check(header.text === expectedHeader, "header count " + count + ": " + header.text)
      check(nodes.text === expectedNodes, "known-node count " + count + ": " + nodes.text)
    }
    check(action.text === (polish ? "Otwórz Meshtastic" : german ? "Meshtastic öffnen" : "Open Meshtastic"), "action label did not update")
    var expectedNumber = ["pl", "de", "fr"].indexOf(language) >= 0 ? "-1234,50"
      : language === "fa" ? "-۱۲۳۴٫۵۰" : "-1234.50"
    check(number.text === expectedNumber, "ungrouped decimal formatting: " + number.text)
    check(bytes.text === (language === "fa" ? "۱۲ / ۲۲۸ B" : "12 / 228 B"), "byte limit digits: " + bytes.text)
    check(Commons.I18n.rightToLeft === (language === "ar" || language === "fa"), "native text direction")
    check(strings.t("missing.plugin.key") === "missing.plugin.key", "missing key fallback")
    check(strings.tn("missing.plugin.key", 2) === "missing.plugin.key", "missing plural fallback")
    stage++
    if (stage === languages.length) {
      console.log("Native localization regressions passed")
      Qt.exit(0)
    } else console.log("LANGUAGE_REQUEST:" + languages[stage])
  }

  property Timer verifier: Timer {
    interval: 100
    repeat: true
    running: true
    onTriggered: {
      if (Commons.I18n.language !== root.languages[root.stage]) return
      try { root.verify() }
      catch (error) { console.error(String(error)); Qt.exit(1) }
    }
  }
}
