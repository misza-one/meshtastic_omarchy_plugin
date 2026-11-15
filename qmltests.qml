import QtQuick
import Quickshell
import "./plugin" as Plugin
import "./plugin/PreviewModel.js" as PreviewModel

ShellRoot {
  Plugin.RadioClient { id: radio }
  property var previewResponse: null
  Connections {
    target: radio
    function onResult(response) {
      if (response.action === "preview_berlin") previewResponse = response.preview
    }
  }

  function check(condition, message) {
    if (!condition) throw new Error(message)
  }

  function state(identity) {
    return {type: "state", connected: true, error: "", device: {region: 3, txEnabled: true},
            channels: [{index: 0, identity: identity, name: "Group", primary: true, unread: 0, encrypted: true}],
            messages: [], sending: false, notificationsEnabled: true}
  }

  Component.onCompleted: Qt.callLater(run)

  function run() {
    try {
      check(radio.utf8Bytes("ż🙂") === 6, "UTF-8 accounting rejected a valid multibyte composition")
      radio.consume(JSON.stringify(state("private-profile")))
      radio.setDraft(0, "private draft <img src='https://example.invalid/tracker'>")
      radio.consume(JSON.stringify(state("public-profile")))
      check(radio.draft(0) === "", "A rekeyed channel exposed the previous private draft")
      radio.setDraft(0, "public draft")
      radio.consume(JSON.stringify(state("private-profile")))
      check(radio.draft(0).startsWith("private draft"), "Switching profiles discarded the private draft")

      radio.pending = {requestId: "failed", channel: 0, identity: "private-profile", text: radio.draft(0)}
      radio.consume(JSON.stringify({type: "result", action: "send", requestId: "failed", ok: false, error: "send_failed"}))
      check(radio.draft(0).startsWith("private draft"), "A failed send discarded the composition")
      check(radio.pending === null && radio.actionError === "send_failed", "Failed send remained busy or hid its error")

      radio.pending = {requestId: "old-profile-send", channel: 0, identity: "private-profile", text: radio.draft(0)}
      radio.consume(JSON.stringify(state("public-profile")))
      radio.consume(JSON.stringify({type: "result", action: "send", requestId: "old-profile-send", ok: true, error: ""}))
      check(radio.draft(0) === "public draft", "A delayed send response cleared another profile's draft")
      radio.consume(JSON.stringify(state("private-profile")))
      check(radio.draft(0) === "", "A successful send left the submitted draft ready to resend")

      var telemetry = state("private-profile")
      telemetry.device = {region: 3, txEnabled: true, transport: "ble", endpoint: "AA:BB:CC:DD:EE:FF",
                          connectedSince: 1700000000, batteryLevel: 101, voltage: 0,
                          channelUtilization: 0, airUtilTx: 0, hopLimit: 0, txPower: 0}
      telemetry.diagnostics = {updatedAt: 1700000001, receivedRadioPackets: 0, heardSinceConnect: 0,
                               queueFree: 0, queueMax: 16, lastRadioRxSnr: 0}
      telemetry.nodes = [{id: "!peer", name: "<b>Not markup</b>", lastHeard: 1700000001, snr: 0,
                          rssi: -90, hopsAway: 0, viaMqtt: true, latitude: 91, longitude: 0}]
      radio.consume(JSON.stringify(telemetry))
      check(radio.device.transport === "ble" && radio.device.endpoint === "AA:BB:CC:DD:EE:FF",
            "Bluetooth connection details were lost")
      check(radio.device.batteryLevel === 101 && radio.device.voltage === 0 && radio.device.hopLimit === 0,
            "Valid external power or zero metrics were changed to unavailable")
      check(radio.diagnostics.receivedRadioPackets === 0 && radio.diagnostics.queueFree === 0,
            "Zero reception or a full queue was changed to unavailable")
      check(radio.nodes[0].snr === 0 && radio.nodes[0].hopsAway === 0 && radio.nodes[0].viaMqtt === true,
            "Peer signal, direct hop count or MQTT source was changed")
      check(radio.nodes[0].latitude === null && radio.nodes[0].longitude === 0,
            "Invalid coordinates were accepted or valid zero longitude was discarded")
      check(radio.nodes[0].name === "<b>Not markup</b>", "A peer name was altered before plain-text rendering")

      var malformed = state("private-profile")
      malformed.device = {region: 3, batteryLevel: null, voltage: "0", uptimeSeconds: -1,
                          channelUtilization: 101, bluetoothEnabled: "false"}
      malformed.diagnostics = {queueFree: null, lastRadioRxSnr: false, receivedRadioPackets: "0"}
      malformed.nodes = [{id: "!missing"}, {id: ""}, null]
      radio.consume(JSON.stringify(malformed))
      check(radio.device.batteryLevel === null && radio.device.voltage === null && radio.device.uptimeSeconds === null
            && radio.device.channelUtilization === null && radio.device.bluetoothEnabled === null,
            "Missing or malformed telemetry became a fabricated value")
      check(radio.diagnostics.queueFree === null && radio.diagnostics.lastRadioRxSnr === null
            && radio.diagnostics.receivedRadioPackets === null, "Malformed diagnostic evidence became zero")
      check(radio.nodes.length === 1 && radio.nodes[0].viaMqtt === null && radio.nodes[0].lastHeard === null,
            "An invalid peer or unknown source/time was presented as valid")

      telemetry.connected = false
      radio.consume(JSON.stringify(telemetry))
      check(radio.device.connectedSince === null && radio.diagnostics.queueFree === null
            && radio.diagnostics.receivedRadioPackets === null && radio.diagnostics.lastRadioRxAt === null,
            "Disconnected session metrics remained current")
      check(radio.nodes.length === 1 && radio.diagnostics.updatedAt === 1700000001,
            "Disconnect discarded timestamped cached evidence")
      telemetry.connected = true
      radio.consume(JSON.stringify(telemetry))
      radio.setDraft(0, "keep after disconnect")
      radio.pending = {requestId: "interrupted", channel: 0, identity: "private-profile", text: radio.draft(0)}
      radio.lost()
      check(radio.draft(0) === "keep after disconnect", "Backend loss discarded the pending composition")
      check(radio.actionError === "send_interrupted", "Backend loss silently reported send success")
      check(radio.device.connectedSince === null && radio.diagnostics.queueFree === undefined,
            "Backend loss retained active connection diagnostics")
      radio.consume(JSON.stringify({type: "result", action: "preview_berlin", ok: true,
        preview: {channels: [{index: 0, name: "MediumFast"}, {index: 1, name: "TEST"}],
                  warnings: ["replaces_channels", "public_key"]}}))
      var previewChannels = PreviewModel.boundedRows(previewResponse.channels, 8)
      var previewWarnings = PreviewModel.boundedRows(previewResponse.warnings, 16)
      check(previewChannels[0].name === "MediumFast" && previewChannels[1].name === "TEST",
            "The configuration preview hid channels crossing the Qt var signal")
      check(previewWarnings[0] === "replaces_channels" && previewWarnings[1] === "public_key",
            "The configuration preview hid replacement or public-key warnings")
      console.log("Meshtastic UI regressions passed")
      Qt.exit(0)
    } catch (error) {
      console.error(String(error))
      Qt.exit(1)
    }
  }
}
