import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root
  visible: false
  property bool available: false
  property bool connected: false
  property string error: "backend_unavailable"
  property var device: ({})
  property var diagnostics: ({})
  property var nodes: []
  property var channels: []
  property var messages: []
  property bool sending: false
  property bool notificationsEnabled: true
  property var drafts: ({})
  property var pending: null
  property string actionError: ""
  property int serial: 0
  signal snapshot()
  signal result(var response)
  signal sent(int channel)

  function localPath(url) {
    return decodeURIComponent(String(url).replace(/^file:\/\//, ""))
  }

  function bounded(value, limit) {
    return typeof value === "string" ? value.slice(0, limit) : ""
  }

  function optionalNumber(value, minimum, maximum, integer) {
    return typeof value === "number" && Number.isFinite(value)
      && value >= minimum && value <= maximum && (!integer || Number.isInteger(value)) ? value : null
  }

  function timestamp(value) { return optionalNumber(value, 1, 8640000000000, true) }

  function optionalBool(value) { return typeof value === "boolean" ? value : null }

  function utf8Bytes(text) {
    var count = 0
    for (var i = 0; i < text.length; i++) {
      var c = text.charCodeAt(i)
      if (c < 0x80) count++
      else if (c < 0x800) count += 2
      else if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length
               && text.charCodeAt(i + 1) >= 0xdc00 && text.charCodeAt(i + 1) <= 0xdfff) {
        count += 4
        i++
      } else count += 3
    }
    return count
  }

  function channelIdentity(channel) {
    var entry = channels.find(function(c) { return c.index === channel })
    return entry ? entry.identity : ""
  }

  function draft(channel) { return drafts[channelIdentity(channel)] || "" }
  function setDraft(channel, text) {
    var identity = channelIdentity(channel)
    if (!identity || drafts[identity] === text) return
    var next = Object.assign({}, drafts)
    next[identity] = text
    drafts = next
  }

  function command(payload) {
    if (!available || !client.running) return false
    client.write(JSON.stringify(payload) + "\n")
    return true
  }

  function request(action, payload) {
    var id = String(Date.now()) + "-" + (++serial)
    var value = Object.assign({}, payload || {}, {action: action, requestId: id})
    return command(value) ? id : ""
  }

  function send(channel) {
    var text = draft(channel)
    var identity = channelIdentity(channel)
    if (!connected || !identity || pending || sending || Number(device.region) === 0
        || !text.trim() || utf8Bytes(text) > 228) return
    var id = String(Date.now()) + "-" + (++serial)
    pending = {requestId: id, channel: channel, identity: identity, text: text}
    actionError = ""
    if (!command({action: "send", channel: channel, channelIdentity: identity, text: text, requestId: id})) {
      pending = null
      actionError = "backend_unavailable"
    }
  }

  function lost() {
    available = false
    connected = false
    error = "backend_unavailable"
    diagnostics = {updatedAt: diagnostics.updatedAt || null}
    device = Object.assign({}, device, {connectedSince: null})
    if (pending) actionError = "send_interrupted"
    pending = null
    sending = false
    snapshot()
  }

  function consume(line) {
    if (line.length > 4194304) return
    var data
    try { data = JSON.parse(line) } catch (_) { return }
    if (!data || typeof data !== "object") return
    if (data.type === "result") {
      if (data.action === "send" && pending && data.requestId === pending.requestId) {
        var submitted = pending
        pending = null
        if (data.ok === true) {
          if ((drafts[submitted.identity] || "") === submitted.text) {
            var next = Object.assign({}, drafts)
            delete next[submitted.identity]
            drafts = next
          }
          actionError = ""
          sent(submitted.channel)
        } else actionError = bounded(data.error, 80) || "send_failed"
      } else if (data.ok !== true && data.action === "notifications") {
        actionError = bounded(data.error, 80) || "backend_unavailable"
      }
      result(data)
      return
    }
    if (data.type !== "state" || !Array.isArray(data.channels) || !Array.isArray(data.messages)) return
    var nextChannels = []
    for (var c = 0; c < Math.min(data.channels.length, 8); c++) {
      var channel = data.channels[c]
      if (!channel || !Number.isInteger(channel.index) || channel.index < 0 || channel.index > 7) continue
      nextChannels.push({index: channel.index, identity: bounded(channel.identity, 128), name: bounded(channel.name, 80),
                        primary: channel.primary === true, unread: Math.max(0, Number(channel.unread) || 0),
                        encrypted: channel.encrypted === true})
    }
    var nextMessages = []
    for (var m = 0; m < Math.min(data.messages.length, 1600); m++) {
      var message = data.messages[m]
      if (!message || !Number.isInteger(message.channel) || !Number.isFinite(Number(message.id))) continue
      nextMessages.push({messageId: String(message.id), channel: message.channel,
                         sender: bounded(message.sender, 160), body: bounded(message.text, 4096),
                         timestamp: Number(message.timestamp) || 0, outgoing: message.outgoing === true,
                         status: bounded(message.status, 40), failure: bounded(message.error, 80)})
    }
    var d = data.device && typeof data.device === "object" ? data.device : {}
    var isConnected = data.connected === true
    device = {name: bounded(d.name, 160), id: bounded(d.id, 40), firmware: bounded(d.firmware, 80),
              port: bounded(d.port, 512), region: optionalNumber(d.region, 0, 255, true),
              regionName: bounded(d.regionName, 80), nodeCount: optionalNumber(d.nodeCount, 0, 1000000, true),
              modemPreset: bounded(d.modemPreset, 80), txEnabled: optionalBool(d.txEnabled),
              transport: d.transport === "usb" || d.transport === "ble" ? d.transport : "",
              endpoint: bounded(d.endpoint, 512), connectedSince: isConnected ? timestamp(d.connectedSince) : null,
              hardwareModel: bounded(d.hardwareModel, 80), role: bounded(d.role, 80),
              frequencySlot: optionalNumber(d.frequencySlot, 0, 100000, true),
              frequencyMHz: optionalNumber(d.frequencyMHz, 0.001, 100000, false),
              hopLimit: optionalNumber(d.hopLimit, 0, 7, true), txPower: optionalNumber(d.txPower, -100, 100, false),
              batteryLevel: optionalNumber(d.batteryLevel, 0, 101, true),
              voltage: optionalNumber(d.voltage, 0, 100, false),
              uptimeSeconds: optionalNumber(d.uptimeSeconds, 0, 4294967295, true),
              channelUtilization: optionalNumber(d.channelUtilization, 0, 100, false),
              airUtilTx: optionalNumber(d.airUtilTx, 0, 100, false), bluetoothEnabled: optionalBool(d.bluetoothEnabled)}
    var sourceDiagnostics = data.diagnostics && typeof data.diagnostics === "object" ? data.diagnostics : {}
    var diag = isConnected ? sourceDiagnostics : {}
    diagnostics = {updatedAt: timestamp(sourceDiagnostics.updatedAt), lastRadioRxAt: timestamp(diag.lastRadioRxAt),
                   lastRadioRxFrom: bounded(diag.lastRadioRxFrom, 160),
                   lastRadioRxSnr: optionalNumber(diag.lastRadioRxSnr, -100, 100, false),
                   lastRadioRxRssi: optionalNumber(diag.lastRadioRxRssi, -300, 100, false),
                   receivedRadioPackets: optionalNumber(diag.receivedRadioPackets, 0, 2147483647, true),
                   heardSinceConnect: optionalNumber(diag.heardSinceConnect, 0, 1000000, true),
                   queueFree: optionalNumber(diag.queueFree, 0, 1000000, true),
                   queueMax: optionalNumber(diag.queueMax, 0, 1000000, true),
                   lastRoutingReason: bounded(diag.lastRoutingReason, 80), lastRoutingAt: timestamp(diag.lastRoutingAt)}
    var nextNodes = []
    var sourceNodes = Array.isArray(data.nodes) ? data.nodes : []
    for (var n = 0; n < Math.min(sourceNodes.length, 100); n++) {
      var node = sourceNodes[n]
      if (!node || typeof node !== "object" || !bounded(node.id, 40)) continue
      nextNodes.push({id: bounded(node.id, 40), name: bounded(node.name, 160),
                      hardwareModel: bounded(node.hardwareModel, 80), role: bounded(node.role, 80),
                      lastHeard: timestamp(node.lastHeard), snr: optionalNumber(node.snr, -100, 100, false),
                      rssi: optionalNumber(node.rssi, -300, 100, false),
                      hopsAway: optionalNumber(node.hopsAway, 0, 7, true), viaMqtt: optionalBool(node.viaMqtt),
                      batteryLevel: optionalNumber(node.batteryLevel, 0, 101, true),
                      voltage: optionalNumber(node.voltage, 0, 100, false),
                      latitude: optionalNumber(node.latitude, -90, 90, false),
                      longitude: optionalNumber(node.longitude, -180, 180, false)})
    }
    nextNodes.sort(function(a, b) { return (b.lastHeard || 0) - (a.lastHeard || 0) })
    nodes = nextNodes
    available = true
    connected = data.connected === true
    error = bounded(data.error, 80)
    channels = nextChannels
    messages = nextMessages
    sending = data.sending === true
    notificationsEnabled = data.notificationsEnabled === true
    snapshot()
  }

  Process {
    id: client
    command: [root.localPath(Qt.resolvedUrl("../bin/python")), root.localPath(Qt.resolvedUrl("bridge.py")), "client"]
    stdinEnabled: true
    stdout: SplitParser { onRead: function(line) { root.consume(String(line)) } }
    onExited: { root.lost(); reconnect.restart() }
    onRunningChanged: if (!running) { root.lost(); reconnect.restart() }
  }

  Timer {
    id: reconnect
    interval: 2500
    repeat: true
    running: true
    onTriggered: if (!client.running) client.running = true
  }

  Component.onCompleted: client.running = true
}
