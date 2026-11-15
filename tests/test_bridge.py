"""Offline regressions: all radio and notification writes are fakes.

Run with: .venv/bin/python -m unittest discover -s tests -v
No test opens a serial port, accesses BlueZ, or transmits over Meshtastic.
"""
import asyncio
import base64
import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import os
import sys
import types
import unittest
from unittest import mock

from meshtastic.protobuf import apponly_pb2, channel_pb2, config_pb2, localonly_pb2
from meshtastic.protobuf import mesh_pb2

SPEC = importlib.util.spec_from_file_location("bridge", Path(__file__).resolve().parents[1] / "plugin/bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


def channel(index=0, name="", key=b"\x01"):
    return channel_pb2.Channel(index=index, role=1 if index == 0 else 2,
                              settings=channel_pb2.ChannelSettings(name=name, psk=key))


def fake_radio():
    config = localonly_pb2.LocalConfig()
    config.lora.CopyFrom(config_pb2.Config.LoRaConfig(use_preset=True, region=3,
        modem_preset=0, tx_enabled=True, hop_limit=3, channel_num=1))
    connected = threading.Event()
    connected.set()
    return types.SimpleNamespace(
        myInfo=types.SimpleNamespace(my_node_num=0xA0B0C0D0),
        metadata=types.SimpleNamespace(firmware_version="2.8.2.test"),
        getMyNodeInfo=lambda: {"user": {"longName": "My radio"}},
        nodesByNum={0xA0B0C0D0: {"user": {"longName": "My radio"}},
                    12: {"user": {"longName": "Alice <&>"}}},
        localNode=types.SimpleNamespace(localConfig=config, channels=[channel(), channel(1, "Private", b"x" * 32)]),
        isConnected=connected,
        sendText=mock.Mock(return_value=types.SimpleNamespace(id=100)),
    )


def packet(packet_id=7, sender=12, index=0, text="Hello", **extra):
    result = {"id": packet_id, "from": sender, "to": bridge.BROADCAST,
              "channel": index, "rxTime": int(time.time()),
              "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": text.encode("utf-8")}}
    result.update(extra)
    return result


def shared_url(config):
    return "https://meshtastic.org/d/#" + base64.urlsafe_b64encode(config.SerializeToString()).decode().rstrip("=")


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.language_file = Path(self.directory.name) / "language"
        language_env = mock.patch.dict(bridge.os.environ, {"OMARCHY_LANGUAGE_FILE": str(self.language_file)})
        language_env.start()
        self.addCleanup(language_env.stop)
        self.path = Path(self.directory.name) / "private/history.sqlite3"
        self.history = bridge.History(self.path)
        self.service = bridge.Bridge(self.history)
        self.radio = fake_radio()
        self.service.interface = self.radio
        self.service.metadata(self.radio, "/dev/test-fake-radio")
        self.service.connected = True
        self.service.error = ""
        self.service.connected_at = int(time.time()) - 1
        self.service.notifications = mock.Mock()

    def tearDown(self):
        self.history.db.close()
        self.directory.cleanup()

    async def send(self, text="Hello", **extra):
        command = dict(action="send", channel=0, channelIdentity=self.service.channels[0]["identity"],
                       text=text, requestId="draft-1")
        command.update(extra)
        return await self.service.command(command)

    async def test_utf8_limit_is_bytes_and_228_is_accepted(self):
        result = await self.send("ą" * 114)
        self.assertTrue(result["ok"])
        self.radio.sendText.assert_called_once_with("ą" * 114, destinationId="^all", channelIndex=0, wantAck=True)
        result = await self.send("ą" * 115)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "message_too_long")
        self.assertEqual(self.radio.sendText.call_count, 1)
        self.assertEqual(self.service.state()["messages"][0]["status"], "queued")

    async def test_invalid_send_has_no_sdk_side_effect(self):
        for text in (None, "", " \n", "has\0nul", "\ud800"):
            result = await self.send(text)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "invalid_text")
        self.radio.sendText.assert_not_called()

    async def test_failed_send_preserves_matching_draft_result_and_does_not_retry(self):
        self.radio.sendText.side_effect = RuntimeError("do not expose SDK diagnostics")
        result = await self.send("keep this draft")
        self.assertEqual(result, {"type": "result", "action": "send", "requestId": "draft-1",
                                  "ok": False, "error": "send_failed"})
        self.assertEqual(self.radio.sendText.call_count, 1)
        message = self.service.state()["messages"][0]
        self.assertEqual((message["text"], message["status"]), ("keep this draft", "failed"))
        self.assertFalse(self.service.sending)
        self.assertEqual(self.service.pending, {})
        await self.service.disconnect()
        self.assertEqual(self.radio.sendText.call_count, 1)

    async def test_offline_unset_region_disabled_tx_and_unknown_channel_block_sends(self):
        self.service.connected = False
        self.assertEqual((await self.send())["error"], "radio_unavailable")
        self.service.connected = True
        self.service.device["region"] = 0
        self.assertEqual((await self.send())["error"], "region_unset")
        self.service.device["region"] = 3
        self.service.device["txEnabled"] = False
        self.assertEqual((await self.send())["error"], "tx_disabled")
        self.service.device["txEnabled"] = True
        result = await self.service.command({"action": "send", "channel": True, "text": "Hi"})
        self.assertEqual(result["error"], "channel_unavailable")
        self.radio.sendText.assert_not_called()

    async def test_receive_dedupes_replays_and_own_echoes(self):
        incoming = packet()
        self.service.receive(incoming)
        self.service.receive(incoming)
        self.assertEqual(len(self.service.state()["messages"]), 1)
        self.assertEqual(self.service.state()["channels"][0]["unread"], 1)
        self.service.notifications.enqueue.assert_called_once()
        self.assertTrue((await self.send())["ok"])
        self.service.receive(packet(packet_id=100, sender=0xA0B0C0D0))
        messages = self.service.state()["messages"]
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[-1]["outgoing"])
        self.assertEqual(messages[-1]["status"], "queued")
        self.assertEqual(self.service.notifications.enqueue.call_count, 1)

    async def test_historical_backlog_is_not_notified_or_marked_unread(self):
        old = packet(rxTime=int(time.time()) - 3600)
        self.service.receive(old)
        self.assertEqual(len(self.service.state()["messages"]), 1)
        self.assertEqual(self.service.state()["channels"][0]["unread"], 0)
        self.service.notifications.enqueue.assert_not_called()
        self.service.receive(packet())
        self.assertEqual(len(self.service.state()["messages"]), 1)
        self.service.notifications.enqueue.assert_not_called()

    async def test_unknown_receive_time_cannot_trigger_replay_toast(self):
        self.service.receive(packet(rxTime=0))
        self.assertEqual(len(self.service.state()["messages"]), 1)
        self.service.notifications.enqueue.assert_not_called()

    async def test_private_messages_and_non_text_never_enter_channel_history(self):
        for destination in (0xA0B0C0D0, 12, 0, None):
            self.service.receive(packet(to=destination))
        self.service.receive(packet(pkiEncrypted=True))
        self.service.receive(packet(decoded={"portnum": "TELEMETRY_APP", "text": "not chat"}))
        self.service.receive(packet(channel=7))
        self.assertEqual(self.service.state()["messages"], [])
        self.service.notifications.enqueue.assert_not_called()

    async def test_channel_history_and_read_counts_are_isolated(self):
        self.service.receive(packet(packet_id=7, index=0, text="zero"))
        self.service.receive(packet(packet_id=8, index=1, text="one"))
        self.assertEqual([c["unread"] for c in self.service.state()["channels"]], [1, 1])
        result = await self.service.command({"action": "read", "channel": 0})
        self.assertTrue(result["ok"])
        self.assertEqual([c["unread"] for c in self.service.state()["channels"]], [0, 1])
        self.service.receive(packet(packet_id=9, index=0, text="new after read"))
        self.assertEqual([c["unread"] for c in self.service.state()["channels"]], [1, 1])
        self.assertEqual([m["channel"] for m in self.service.state()["messages"]], [0, 1, 0])

    async def test_channel_key_and_name_changes_do_not_leak_previous_profile(self):
        self.service.receive(packet())
        original = self.service.channels[0]["identity"]
        self.radio.localNode.channels[0].settings.psk = b"z" * 32
        self.service.metadata(self.radio, "/dev/test-fake-radio")
        self.assertNotEqual(original, self.service.channels[0]["identity"])
        self.assertEqual(self.service.state()["messages"], [])
        self.radio.localNode.channels[0].settings.psk = b"\x01"
        self.service.metadata(self.radio, "/dev/test-fake-radio")
        self.assertEqual(len(self.service.state()["messages"]), 1)
        self.radio.localNode.channels[0].settings.name = "renamed"
        self.service.metadata(self.radio, "/dev/test-fake-radio")
        self.assertEqual(self.service.state()["messages"], [])

    async def test_stale_or_missing_channel_identity_blocks_send(self):
        original = self.service.channels[0]["identity"]
        self.radio.localNode.channels[0].settings.psk = b"n" * 32
        self.service.metadata(self.radio, "/dev/test-fake-radio")
        for identity in (None, original, ""):
            result = await self.send(channelIdentity=identity)
            self.assertEqual(result["error"], "channel_unavailable")
        self.radio.sendText.assert_not_called()

    async def test_routing_ack_is_distinct_from_echo_or_recipient_read(self):
        self.assertTrue((await self.send())["ok"])
        self.service.receive(packet(packet_id=101, sender=0xA0B0C0D0))
        self.assertEqual(self.service.state()["messages"][0]["status"], "queued")
        self.service.receive({"decoded": {"portnum": "ROUTING_APP", "requestId": 100, "routing": {}}})
        message = self.service.state()["messages"][0]
        self.assertEqual(message["status"], "acknowledged")
        self.assertEqual(message["error"], "")
        self.assertEqual(self.service.pending, {})

    async def test_negative_ack_and_disconnect_are_honest(self):
        await self.send()
        self.service.receive({"decoded": {"portnum": "ROUTING_APP", "requestId": 100,
                                          "routing": {"errorReason": "NO_ROUTE"}}})
        self.assertEqual(self.service.state()["messages"][0]["status"], "failed")
        self.radio.sendText.return_value = types.SimpleNamespace(id=101)
        await self.send("second")
        await self.service.disconnect()
        self.assertEqual(self.service.state()["messages"][-1]["status"], "unconfirmed")
        self.assertFalse(self.service.state()["connected"])
        self.assertEqual(self.radio.sendText.call_count, 2)

    async def test_concurrent_send_is_rejected_not_queued(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return types.SimpleNamespace(id=100)
        self.radio.sendText.side_effect = delayed
        first = asyncio.create_task(self.send())
        try:
            await asyncio.to_thread(entered.wait, 2)
            second = await self.send("duplicate")
            self.assertEqual(second["error"], "backend_busy")
        finally:
            release.set()
        self.assertTrue((await first)["ok"])
        self.assertEqual(self.radio.sendText.call_count, 1)

    async def test_muting_and_snapshot_are_persistent_and_private(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        result = await self.service.command({"action": "notifications", "enabled": False})
        self.assertTrue(result["ok"])
        restored = bridge.Bridge(self.history)
        self.assertFalse(restored.state()["notificationsEnabled"])
        self.assertEqual(restored.state()["channels"][0]["name"], "LongFast")
        serialized = bridge.encode(restored.state()).decode()
        self.assertNotIn("_key", serialized)
        self.assertNotIn("psk", serialized)
        self.assertNotIn("xxxxxxxxxxxxxxxx", serialized)

    async def test_only_latest_200_per_channel_are_emitted_but_history_remains(self):
        for index in range(205):
            self.service.receive(packet(packet_id=index + 1000, text=str(index)))
        state = self.service.state()
        self.assertEqual(len(state["messages"]), 200)
        self.assertEqual(state["messages"][0]["text"], "5")
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 205)

    async def test_multiple_clients_receive_same_state_and_command_results(self):
        path = str(Path(self.directory.name) / "test.sock")
        server = await asyncio.start_unix_server(lambda r, w: bridge.serve_client(self.service, r, w),
                                                 path=path, limit=bridge.MAX_COMMAND_BYTES)
        clients = []
        try:
            for _ in range(2):
                reader, writer = await asyncio.open_unix_connection(path)
                clients.append((reader, writer))
                state = json.loads(await asyncio.wait_for(reader.readline(), 2))
                self.assertEqual(state["type"], "state")
                self.assertTrue(state["connected"])
            clients[0][1].write(bridge.encode({"action": "notifications", "enabled": False, "requestId": "mute"}))
            await clients[0][1].drain()
            state = json.loads(await asyncio.wait_for(clients[1][0].readline(), 2))
            self.assertFalse(state["notificationsEnabled"])
            first_state = json.loads(await asyncio.wait_for(clients[0][0].readline(), 2))
            self.assertFalse(first_state["notificationsEnabled"])
            result = json.loads(await asyncio.wait_for(clients[0][0].readline(), 2))
            self.assertEqual((result["type"], result["requestId"], result["ok"]), ("result", "mute", True))
        finally:
            for _, writer in clients:
                writer.close()
                await writer.wait_closed()
            server.close()
            await server.wait_closed()

    async def test_stdio_client_keeps_results_when_stdin_reaches_eof(self):
        path = str(Path(self.directory.name) / "omarchy-meshtastic.sock")
        server = await asyncio.start_unix_server(lambda r, w: bridge.serve_client(self.service, r, w),
                                                 path=path, limit=bridge.MAX_COMMAND_BYTES)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, str(Path(bridge.__file__)), "client",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=dict(os.environ, XDG_RUNTIME_DIR=self.directory.name))
            stdout, stderr = await asyncio.wait_for(process.communicate(bridge.encode(
                {"action": "notifications", "enabled": False, "requestId": "eof"})), 5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            output = [json.loads(line) for line in stdout.splitlines()]
            self.assertEqual(output[0]["type"], "state")
            self.assertEqual(output[-1]["requestId"], "eof")
            self.assertTrue(output[-1]["ok"])
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            server.close()
            await server.wait_closed()

    async def test_preview_is_safe_offline_and_keys_never_appear_in_result_or_db(self):
        self.service.connected = False
        config = apponly_pb2.ChannelSet(settings=[channel(key=b"s" * 32).settings])
        config.lora_config.use_preset = True
        config.lora_config.modem_preset = 4
        url = shared_url(config)
        with self.assertNoLogs(bridge.LOG):
            result = await self.service.command(
                {"action": "preview_config", "value": "  " + url + "  ", "channelName": ""}, "client-a")
        self.assertTrue(result["ok"])
        self.assertEqual(result["preview"]["regionName"], "EU_868")
        self.assertTrue(result["preview"]["txEnabled"])
        self.assertIn("offline_preview", result["preview"]["warnings"])
        self.assertIn("region_preserved", result["preview"]["warnings"])
        self.assertEqual(result["preview"]["operation"], "replace")
        self.assertEqual(result["preview"]["targetName"], "")
        self.assertIsNone(result["preview"]["targetIndex"])
        self.assertNotIn(url, bridge.encode(result).decode())
        self.assertNotIn(url, "\n".join(self.history.db.iterdump()))
        self.assertNotIn("ssssssss", bridge.encode(result).decode())
        self.assertNotIn("ssssssss", "\n".join(self.history.db.iterdump()))
        self.radio.sendText.assert_not_called()

    async def test_berlin_preview_contains_only_public_mediumfast_and_test(self):
        result = await self.service.command({"action": "preview_berlin", "requestId": "berlin"}, "a")
        self.assertTrue(result["ok"])
        preview = result["preview"]
        self.assertEqual([c["name"] for c in preview["channels"]], ["MediumFast", "TEST"])
        self.assertEqual(preview["modemPreset"], "MediumFast")
        self.assertEqual(preview["frequencySlot"], 1)
        self.assertEqual(preview["regionName"], "EU_868")
        self.assertIn("public_key", preview["warnings"])
        self.assertNotIn("SOS", str(preview))
        self.radio.sendText.assert_not_called()

    async def test_preview_token_is_client_bound_confirmed_expiring_and_single_use(self):
        preview = await self.service.command({"action": "preview_berlin"}, "a")
        token = preview["preview"]["token"]
        apply = {"action": "apply_config", "token": token, "confirmed": True, "requestId": "apply"}
        self.assertEqual((await self.service.command(apply, "b"))["error"], "preview_expired")
        self.assertEqual((await self.service.command(dict(apply, confirmed=False), "a"))["error"], "confirmation_required")
        with mock.patch.object(bridge, "write_config") as writer:
            result = await self.service.command(apply, "a")
            self.assertTrue(result["ok"])
            writer.assert_called_once()
        self.assertFalse(self.service.connected)
        self.assertEqual((await self.service.command(apply, "a"))["error"], "preview_expired")
        self.radio.sendText.assert_not_called()

    async def test_expired_or_reconnected_preview_cannot_write(self):
        for mode in ("expiry", "generation"):
            result = await self.service.command({"action": "preview_berlin"}, "a")
            preview = self.service.previews["a"]
            if mode == "expiry":
                preview.expires_at = 0
            else:
                self.service.generation += 1
            with mock.patch.object(bridge, "write_config") as writer:
                result = await self.service.command({"action": "apply_config", "confirmed": True,
                                                    "token": result["preview"]["token"]}, "a")
                self.assertEqual(result["error"], "preview_expired")
                writer.assert_not_called()

    async def test_partial_config_failure_is_not_retried_and_forces_reload(self):
        result = await self.service.command({"action": "preview_berlin"}, "a")
        with mock.patch.object(bridge, "write_config", side_effect=RuntimeError("partial")) as writer:
            result = await self.service.command({"action": "apply_config", "confirmed": True,
                                                "token": result["preview"]["token"]}, "a")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "config_failed")
            self.assertFalse(self.service.connected)
            self.assertTrue(self.service.reconnect.is_set())
            self.assertEqual(writer.call_count, 1)

    async def test_full_url_still_explicitly_replaces_all_channels(self):
        config = apponly_pb2.ChannelSet(settings=[channel(name="Imported", key=b"z" * 16).settings])
        config.lora_config.use_preset = True
        config.lora_config.modem_preset = config_pb2.Config.LoRaConfig.MEDIUM_FAST
        result = await self.service.command({"action": "preview_config", "value": shared_url(config),
                                            "channelName": "IgnoredForUrl"}, "url-client")
        self.assertTrue(result["ok"])
        preview = result["preview"]
        self.assertEqual(preview["operation"], "replace")
        self.assertEqual([row["name"] for row in preview["channels"]], ["Imported"])
        self.assertIn("replaces_channels", preview["warnings"])
        with mock.patch.object(bridge, "write_config") as writer, mock.patch.object(bridge, "write_channel") as single:
            applied = await self.service.command({"action": "apply_config", "confirmed": True,
                                                 "token": preview["token"]}, "url-client")
        self.assertTrue(applied["ok"])
        writer.assert_called_once()
        single.assert_not_called()
        self.assertEqual(writer.call_args.args[1][0].settings, config.settings[0])
        self.assertFalse(self.service.connected)
        self.assertTrue(self.service.reconnect.is_set())

    async def test_bare_key_input_trims_outer_whitespace_and_accepts_urlsafe_unpadded_key(self):
        key = b"\xff" * 16
        result = await self.service.command({"action": "preview_config",
            "value": " \n" + base64.urlsafe_b64encode(key).decode().rstrip("=") + "\t ",
            "channelName": "abc" + "ą" * 4}, "key-client")
        self.assertTrue(result["ok"])
        self.assertEqual(result["preview"]["targetName"], "abc" + "ą" * 4)
        self.assertEqual(self.service.previews["key-client"].channels[2].settings.psk, key)

    async def preview_key(self, name="BerlinMesh", key=b"k" * 32, client="key-client"):
        return await self.service.command({"action": "preview_config",
            "value": base64.b64encode(key).decode(), "channelName": name}, client)

    def prepare_config_writer(self):
        node = self.radio.localNode
        for method in ("ensureSessionKey", "beginSettingsTransaction", "writeChannel",
                       "writeConfig", "commitSettingsTransaction"):
            setattr(node, method, mock.Mock())
        self.radio.nodesByNum[self.radio.myInfo.my_node_num]["adminSessionPassKey"] = b"fake-session"
        return node

    async def test_key_preview_adds_from_raw_channels_without_changing_radio_or_persisting_secrets(self):
        node = self.prepare_config_writer()
        node.channels[1].settings.uplink_enabled = True
        node.channels[1].settings.module_settings.position_precision = 13
        node.localConfig.lora.override_frequency = 869.5
        node.localConfig.lora.frequency_offset = 0.25
        before_channels = [channel.SerializeToString() for channel in node.channels]
        before_lora = node.localConfig.lora.SerializeToString()
        self.service.channels[1]["name"] = "StaleCache"
        key = b"new-secret-key-material-32-bytes"
        self.assertEqual(len(key), 32)
        with self.assertNoLogs(bridge.LOG):
            result = await self.preview_key(key=key)
        self.assertTrue(result["ok"])
        public = result["preview"]
        self.assertEqual((public["operation"], public["targetName"], public["targetIndex"]),
                         ("add", "BerlinMesh", 2))
        self.assertEqual([channel["name"] for channel in public["channels"]],
                         ["LongFast", "Private", "BerlinMesh"])
        self.assertEqual(public["modemPreset"], "LongFast")
        self.assertEqual(public["frequencySlot"], 1)
        self.assertIn("other_channels_preserved", public["warnings"])
        self.assertIn("berlin_profile_mismatch", public["warnings"])
        self.assertNotIn("replaces_channels", public["warnings"])
        self.assertNotIn("public_key", public["warnings"])
        self.assertNotIn("uplink_enabled", public["warnings"])
        stored = self.service.previews["key-client"]
        self.assertEqual(stored.lora.SerializeToString(), before_lora)
        self.assertEqual([channel.SerializeToString() for channel in stored.channels[:2]], before_channels)
        self.assertEqual(stored.channels[2].settings.psk, key)
        self.assertFalse(stored.channels[2].settings.uplink_enabled)
        self.assertFalse(stored.channels[2].settings.downlink_enabled)
        self.assertEqual(stored.channels[2].settings.module_settings.position_precision, 0)
        self.assertEqual([channel.SerializeToString() for channel in node.channels], before_channels)
        self.assertEqual(node.localConfig.lora.SerializeToString(), before_lora)
        for secret in (key.decode(), base64.b64encode(key).decode(),
                       base64.b64encode(b"x" * 32).decode(), key.hex()):
            self.assertNotIn(secret, bridge.encode(result).decode())
            self.assertNotIn(secret, repr(stored))
            self.assertNotIn(secret, "\n".join(self.history.db.iterdump()))
        node.beginSettingsTransaction.assert_not_called()
        node.writeChannel.assert_not_called()
        node.writeConfig.assert_not_called()
        self.radio.sendText.assert_not_called()

    async def test_key_update_matches_exact_effective_name_and_preserves_target_settings(self):
        target = self.radio.localNode.channels[1]
        target.settings.uplink_enabled = True
        target.settings.downlink_enabled = True
        target.settings.id = 123
        target.settings.module_settings.position_precision = 14
        before = channel_pb2.Channel()
        before.CopyFrom(target)
        result = await self.preview_key("Private", b"\x01")
        self.assertTrue(result["ok"])
        self.assertEqual(result["preview"]["operation"], "update")
        self.assertEqual(result["preview"]["targetIndex"], 1)
        self.assertEqual(set(result["preview"]["warnings"]),
                         {"other_channels_preserved", "public_key", "uplink_enabled", "downlink_enabled"})
        expected = channel_pb2.Channel()
        expected.CopyFrom(before)
        expected.settings.psk = b"\x01"
        self.assertEqual(self.service.previews["key-client"].channels[1], expected)
        self.assertEqual(target, before)
        different_case = await self.preview_key("private", b"k" * 16)
        self.assertEqual(different_case["preview"]["operation"], "add")
        self.assertEqual(different_case["preview"]["targetIndex"], 2)
        primary = await self.preview_key("LongFast", b"k" * 16)
        self.assertEqual(primary["preview"]["operation"], "update")
        self.assertEqual(primary["preview"]["targetIndex"], 0)
        self.assertEqual(self.service.previews["key-client"].channels[0].settings.name, "")
        self.assertEqual(self.service.previews["key-client"].channels[0].role, 1)

    async def test_key_berlin_profile_warning_reports_mismatch_without_switching(self):
        lora = self.radio.localNode.localConfig.lora
        lora.modem_preset = config_pb2.Config.LoRaConfig.MEDIUM_FAST
        matching = await self.preview_key()
        self.assertNotIn("berlin_profile_mismatch", matching["preview"]["warnings"])
        for field, value in (("region", 2), ("modem_preset", 0), ("channel_num", 0),
                             ("use_preset", False), ("override_frequency", 869.5),
                             ("frequency_offset", 0.25)):
            with self.subTest(field=field):
                old = getattr(lora, field)
                setattr(lora, field, value)
                before = lora.SerializeToString()
                result = await self.preview_key()
                self.assertTrue(result["ok"])
                self.assertIn("berlin_profile_mismatch", result["preview"]["warnings"])
                self.assertEqual(self.service.previews["key-client"].lora.SerializeToString(), before)
                self.assertEqual(lora.SerializeToString(), before)
                setattr(lora, field, old)

    async def test_invalid_key_or_channel_name_fails_before_any_writer(self):
        node = self.prepare_config_writer()
        valid = base64.b64encode(b"k" * 32).decode()
        inputs = [(value, "BerlinMesh", "invalid_key") for value in
                  (None, "", "none", "passphrase", "a" * 64, "%%%=", "AQ==suffix",
                   base64.b64encode(b"\x0b").decode(), base64.b64encode(b"x" * 15).decode())]
        inputs += [(valid, name, "invalid_channel_name") for name in
                   (None, "", "   ", "abcdefghijkl", "ą" * 6, "line\nname", "tab\tname",
                    "nul\x00name", "del\x7fname", "c1\x85name", "\ud800")]
        for value, name, error in inputs:
            with self.subTest(value=value, name=name):
                result = await self.service.command(
                    {"action": "preview_config", "value": value, "channelName": name}, "invalid")
                self.assertEqual(result["error"], error)
                self.assertNotIn("preview", result)
                self.assertNotIn("invalid", self.service.previews)
        legacy = await self.service.command({"action": "preview_config", "url": shared_url(bridge.berlin_config())})
        self.assertEqual(legacy["error"], "invalid_key")
        node.beginSettingsTransaction.assert_not_called()
        node.writeChannel.assert_not_called()
        node.writeConfig.assert_not_called()

    async def test_key_preview_requires_live_radio_even_when_cached_channels_exist(self):
        for mode in ("disconnected", "missing_interface", "transport_lost"):
            with self.subTest(mode=mode):
                self.service.connected = mode != "disconnected"
                self.service.interface = None if mode == "missing_interface" else self.radio
                if mode == "transport_lost":
                    self.radio.isConnected.clear()
                result = await self.preview_key()
                self.assertEqual(result["error"], "radio_unavailable")
                self.assertNotIn("preview", result)
        self.assertFalse(self.service.previews)

    async def test_full_channels_reject_add_but_allow_update_and_duplicate_names_reject(self):
        self.radio.localNode.channels = [channel(index, "C" + str(index)) for index in range(8)]
        result = await self.preview_key()
        self.assertEqual(result["error"], "channels_full")
        update = await self.preview_key("C7")
        self.assertEqual(update["preview"]["operation"], "update")
        self.assertEqual(update["preview"]["targetIndex"], 7)
        self.radio.localNode.channels[7].settings.name = "C1"
        ambiguous = await self.preview_key("C1")
        self.assertEqual(ambiguous["error"], "channel_ambiguous")
        self.radio.localNode.channels[0].settings.name = ""
        self.radio.localNode.channels[1].settings.name = "LongFast"
        effective_ambiguous = await self.preview_key("LongFast")
        self.assertEqual(effective_ambiguous["error"], "channel_ambiguous")

    async def test_add_uses_first_disabled_slot_without_overwriting_active_channels(self):
        self.radio.localNode.channels = [channel(), channel_pb2.Channel(index=1, role=0),
                                         channel(2, "Keep"), channel(7, "Last")]
        result = await self.preview_key("New")
        self.assertTrue(result["ok"])
        self.assertEqual(result["preview"]["targetIndex"], 1)
        self.assertEqual([row["index"] for row in result["preview"]["channels"]], [0, 1, 2, 7])
        self.assertEqual(self.radio.localNode.channels[1].role, 0)

    async def test_key_apply_writes_only_target_and_preserves_latest_other_channels_and_lora(self):
        for name, operation, index in (("BerlinMesh", "add", 2), ("Private", "update", 1)):
            with self.subTest(operation=operation):
                self.radio = fake_radio()
                self.service.interface = self.radio
                self.service.connected = True
                self.service.reconnect.clear()
                node = self.prepare_config_writer()
                result = await self.preview_key(name)
                self.assertEqual(result["preview"]["operation"], operation)
                # Changes to unrelated settings after preview must survive too.
                node.channels[0].settings.id = 987
                node.localConfig.lora.hop_limit = 6
                node.localConfig.lora.tx_enabled = False
                before_lora = node.localConfig.lora.SerializeToString()
                before_other = {row.index: row.SerializeToString() for row in node.channels if row.index != index}
                applied = await self.service.command({"action": "apply_config", "confirmed": True,
                    "token": result["preview"]["token"]}, "key-client")
                self.assertTrue(applied["ok"])
                node.ensureSessionKey.assert_called_once_with()
                node.beginSettingsTransaction.assert_called_once_with()
                node.writeChannel.assert_called_once_with(index)
                node.commitSettingsTransaction.assert_called_once_with()
                node.writeConfig.assert_not_called()
                self.assertEqual(node.channels[index].settings.psk, b"k" * 32)
                self.assertEqual(node.localConfig.lora.SerializeToString(), before_lora)
                self.assertEqual({row.index: row.SerializeToString() for row in node.channels
                                  if row.index in before_other}, before_other)
                self.assertFalse(self.service.connected)
                self.assertTrue(self.service.reconnect.is_set())
                replay = await self.service.command({"action": "apply_config", "confirmed": True,
                    "token": result["preview"]["token"]}, "key-client")
                self.assertEqual(replay["error"], "preview_expired")
                node.writeChannel.assert_called_once_with(index)
                self.radio.sendText.assert_not_called()

    async def test_stale_key_targets_never_write_or_retry(self):
        for name, change in (("BerlinMesh", "occupied"), ("Private", "key"),
                             ("Private", "settings"), ("Private", "removed")):
            with self.subTest(change=change):
                self.radio = fake_radio()
                self.service.interface = self.radio
                node = self.prepare_config_writer()
                preview = await self.preview_key(name)
                if change == "occupied":
                    node.channels.append(channel(2, "Unexpected"))
                elif change == "key":
                    node.channels[1].settings.psk = b"z" * 32
                elif change == "settings":
                    node.channels[1].settings.downlink_enabled = True
                else:
                    node.channels.pop()
                command = {"action": "apply_config", "confirmed": True, "token": preview["preview"]["token"]}
                result = await self.service.command(command, "key-client")
                self.assertEqual(result["error"], "preview_expired")
                self.assertEqual((await self.service.command(command, "key-client"))["error"], "preview_expired")
                node.beginSettingsTransaction.assert_not_called()
                node.writeChannel.assert_not_called()
                node.writeConfig.assert_not_called()

    async def test_key_tokens_require_confirmation_client_radio_generation_and_expiry(self):
        node = self.prepare_config_writer()
        for mode in ("confirmation", "client", "token", "radio", "generation", "expiry"):
            with self.subTest(mode=mode):
                result = await self.preview_key()
                stored = self.service.previews["key-client"]
                command = {"action": "apply_config", "confirmed": True, "token": result["preview"]["token"]}
                client = "key-client"
                expected_error = "preview_expired"
                if mode == "confirmation":
                    command["confirmed"] = False
                    expected_error = "confirmation_required"
                elif mode == "client":
                    client = "other"
                elif mode == "token":
                    command["token"] = "incorrect"
                elif mode == "radio":
                    stored.radio_id = "!different"
                elif mode == "generation":
                    stored.generation -= 1
                else:
                    stored.expires_at = 0
                self.assertEqual((await self.service.command(command, client))["error"], expected_error)
        node.beginSettingsTransaction.assert_not_called()
        node.writeChannel.assert_not_called()
        node.writeConfig.assert_not_called()

    async def test_key_partial_failure_forces_reload_without_retry_or_lora_write(self):
        node = self.prepare_config_writer()
        node.writeChannel.side_effect = RuntimeError("possibly accepted")
        result = await self.preview_key()
        command = {"action": "apply_config", "confirmed": True, "token": result["preview"]["token"]}
        failed = await self.service.command(command, "key-client")
        self.assertEqual(failed["error"], "config_failed")
        self.assertFalse(self.service.connected)
        self.assertTrue(self.service.reconnect.is_set())
        self.assertEqual((await self.service.command(command, "key-client"))["error"], "preview_expired")
        node.writeChannel.assert_called_once_with(2)
        node.writeConfig.assert_not_called()
        node.commitSettingsTransaction.assert_not_called()

    async def test_ack_timeout_is_unconfirmed_without_resending(self):
        await self.send()
        message_id, _ = self.service.pending[100]
        self.service.pending[100] = (message_id, 0)
        self.service.expire_acks()
        self.assertEqual(self.service.state()["messages"][0]["status"], "unconfirmed")
        self.assertEqual(self.service.state()["messages"][0]["error"], "ack_timeout")
        self.assertEqual(self.radio.sendText.call_count, 1)

    async def test_notification_action_opens_only_fixed_plugin_command(self):
        selected = types.SimpleNamespace(returncode=0,
            communicate=mock.AsyncMock(return_value=(b"default\n", b"")))
        opened = asyncio.Event()
        opener = types.SimpleNamespace(returncode=0, wait=mock.AsyncMock(return_value=0))
        async def spawn(*args, **kwargs):
            if args[0] == "omarchy-shell":
                opened.set()
                return opener
            return selected
        self.language_file.write_text("pl\n", encoding="utf-8")
        notifications = bridge.Notifications(lambda: True)
        notifications.enqueue("$(bad)", "channel", "<unsafe>")
        self.language_file.write_text("de\n", encoding="utf-8")
        with mock.patch.object(bridge.asyncio, "create_subprocess_exec", side_effect=spawn) as execute:
            task = asyncio.create_task(notifications.run())
            try:
                await asyncio.wait_for(opened.wait(), 2)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self.assertIn("--action=default=Meshtastic öffnen", execute.call_args_list[0].args)
            self.assertEqual(execute.call_args_list[1].args,
                             ("omarchy-shell", "shell", "summon", "miszarchy.meshtastic"))

    async def test_running_notification_worker_follows_live_language(self):
        self.language_file.write_text("en\n", encoding="utf-8")
        notifications = bridge.Notifications(lambda: True)
        delivered = asyncio.Event()
        labels = []
        async def spawn(*args, **kwargs):
            labels.append(next(arg for arg in args if arg.startswith("--action=")))
            if len(labels) == 1:
                self.language_file.write_text("pl\n", encoding="utf-8")
                notifications.enqueue("sender", "channel", "second message")
            else:
                delivered.set()
            return types.SimpleNamespace(returncode=0,
                communicate=mock.AsyncMock(return_value=(b"", b"")))
        notifications.enqueue("sender", "channel", "first message")
        with mock.patch.object(bridge.asyncio, "create_subprocess_exec", side_effect=spawn):
            worker = asyncio.create_task(notifications.run())
            try:
                await asyncio.wait_for(delivered.wait(), 2)
            finally:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
        self.assertEqual(labels, ["--action=default=Open Meshtastic", "--action=default=Otwórz Meshtastic"])

    async def test_notification_queue_is_bounded_and_muting_stops_new_toasts(self):
        enabled = True
        notifications = bridge.Notifications(lambda: enabled)
        for _ in range(20):
            notifications.enqueue("sender", "channel", "text")
        self.assertEqual(notifications.queue.qsize(), 16)
        enabled = False
        notifications.queue.get_nowait()
        notifications.enqueue("sender", "channel", "text")
        self.assertEqual(notifications.queue.qsize(), 15)

    async def test_transport_switch_keeps_radio_channel_history_and_failure_status(self):
        self.radio.sendText.side_effect = RuntimeError("local write failed")
        await self.send("retained draft text")
        identity = self.service.channels[0]["identity"]
        self.service.receive(packet(rxSnr=0, rxRssi=-70))
        self.assertEqual(self.service.diagnostics["receivedRadioPackets"], 1)
        await self.service.disconnect()
        self.service.metadata(self.radio, "AA:BB:CC:DD:EE:FF", "ble")
        state = self.service.state()
        self.assertEqual(state["channels"][0]["identity"], identity)
        self.assertEqual(state["device"]["id"], "!a0b0c0d0")
        self.assertEqual(state["device"]["transport"], "ble")
        self.assertEqual(state["device"]["endpoint"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(state["device"]["port"], "")
        self.assertEqual(state["messages"][0]["status"], "failed")
        self.assertEqual(state["messages"][0]["text"], "retained draft text")
        self.assertEqual(state["diagnostics"]["receivedRadioPackets"], 0)
        self.assertEqual(state["diagnostics"]["heardSinceConnect"], 0)
        self.assertIsNone(state["diagnostics"]["lastRadioRxAt"])
        self.assertIsNone(state["device"]["connectedSince"])
        self.assertEqual(self.radio.sendText.call_count, 1)

    async def test_diagnostics_missing_zero_and_external_power_are_distinct(self):
        state = self.service.state()
        for field in ("batteryLevel", "voltage", "uptimeSeconds", "channelUtilization",
                      "airUtilTx", "bluetoothEnabled", "frequencyMHz"):
            self.assertIsNone(state["device"][field], field)
        local = self.radio.nodesByNum[self.radio.myInfo.my_node_num]
        local["deviceMetrics"] = {"batteryLevel": 101, "voltage": 4.326, "uptimeSeconds": 0,
                                 "channelUtilization": 0, "airUtilTx": 0}
        self.radio.localNode.localConfig.bluetooth.enabled = False
        self.radio.localNode.localConfig.device.SetInParent()
        self.service.refresh_diagnostics(self.radio)
        device = self.service.state()["device"]
        self.assertEqual(device["batteryLevel"], 101)
        self.assertEqual(device["voltage"], 4.326)
        self.assertEqual(device["uptimeSeconds"], 0)
        self.assertEqual(device["channelUtilization"], 0)
        self.assertEqual(device["airUtilTx"], 0)
        self.assertFalse(device["bluetoothEnabled"])
        self.assertEqual(device["role"], "CLIENT")
        local["deviceMetrics"] = {"batteryLevel": True, "voltage": float("nan"),
                                 "uptimeSeconds": -1, "channelUtilization": float("inf"),
                                 "airUtilTx": 10 ** 1000}
        self.service.refresh_diagnostics(self.radio)
        for field in ("batteryLevel", "voltage", "uptimeSeconds", "channelUtilization", "airUtilTx"):
            self.assertIsNone(self.service.device[field], field)
        self.radio.sendText.assert_not_called()

    async def test_fresh_receive_counters_exclude_cache_mqtt_backlog_and_own_echo(self):
        now = int(time.time())
        self.radio.nodesByNum[12]["lastHeard"] = now
        self.service.refresh_diagnostics(self.radio)
        self.assertEqual(self.service.diagnostics["heardSinceConnect"], 0)
        for incoming in (packet(packet_id=1, viaMqtt=True),
                         packet(packet_id=2, sender=self.radio.myInfo.my_node_num),
                         packet(packet_id=3, rxTime=now - 3600), packet(packet_id=4, rxTime=0),
                         packet(packet_id=5, rxTime=now + 300)):
            self.service.receive(incoming)
        self.assertEqual(self.service.diagnostics["receivedRadioPackets"], 0)
        telemetry = packet(packet_id=6, rxSnr=0, rxRssi=-80,
                           decoded={"portnum": "TELEMETRY_APP", "telemetry": {"deviceMetrics": {}}})
        self.service.receive(telemetry)
        self.service.receive(telemetry)
        self.service.receive(packet(packet_id=7, sender=13, decoded={"portnum": "POSITION_APP"}))
        diagnostics = self.service.diagnostics
        self.assertEqual(diagnostics["receivedRadioPackets"], 2)
        self.assertEqual(diagnostics["heardSinceConnect"], 2)
        self.assertEqual(diagnostics["lastRadioRxFrom"], "!0000000d")
        self.assertIsNone(diagnostics["lastRadioRxSnr"])
        self.assertIsNone(diagnostics["lastRadioRxRssi"])
        self.assertEqual(diagnostics["lastRadioRxAt"], now)
        self.radio.sendText.assert_not_called()

    async def test_passive_nontext_refreshes_metrics_without_sqlite_or_identity_rewrites(self):
        local = self.radio.nodesByNum[self.radio.myInfo.my_node_num]
        local["deviceMetrics"] = {"batteryLevel": 0, "uptimeSeconds": 17}
        self.radio.nodesByNum[12]["position"] = {"latitudeI": 525200000, "longitudeI": 134050000}
        self.radio.nodesByNum[12]["viaMqtt"] = True
        identity = self.service.channels[0]["identity"]
        with mock.patch.object(self.history, "put") as save, \
                mock.patch.object(bridge, "channel_identity") as hash_channel:
            self.service.receive(packet(decoded={"portnum": "POSITION_APP"}, rxSnr=0, rxRssi=-70,
                                        hopStart=3, hopLimit=1))
            save.assert_not_called()
            hash_channel.assert_not_called()
        self.assertEqual(self.service.device["batteryLevel"], 0)
        self.assertEqual(self.service.device["uptimeSeconds"], 17)
        self.assertEqual(self.service.channels[0]["identity"], identity)
        peer = self.service.nodes[0]
        self.assertAlmostEqual(peer["latitude"], 52.52)
        self.assertAlmostEqual(peer["longitude"], 13.405)
        self.assertEqual(peer["hopsAway"], 2)
        self.assertEqual(peer["snr"], 0)
        self.assertEqual(peer["rssi"], -70)
        self.assertFalse(peer["viaMqtt"])
        self.assertEqual(self.service.state()["messages"], [])
        updated = self.service.diagnostics["updatedAt"]
        await self.service.disconnect()
        with mock.patch.object(bridge.time, "time", return_value=time.time() + 1000):
            restored = bridge.Bridge(self.history)
            self.assertEqual(restored.state()["diagnostics"]["updatedAt"], updated)
            self.assertIsNone(restored.state()["device"]["connectedSince"])
        self.radio.sendText.assert_not_called()

    async def test_peer_list_is_bounded_sorted_whitelisted_and_has_no_online_claim(self):
        now = int(time.time())
        for index in range(1, 121):
            self.radio.nodesByNum[index] = {"user": {"longName": "<b>" + "x" * 200,
                "hwModel": "HELTEC_V4", "role": "CLIENT", "publicKey": "private-user-key"},
                "lastHeard": now - index, "snr": float("nan"), "rssi": -999,
                "hopsAway": 99, "viaMqtt": index == 1, "adminSessionPassKey": "private-session",
                "deviceMetrics": {"batteryLevel": 101, "voltage": float("inf")},
                "position": {"latitude": 91, "longitude": 13.4}}
        self.radio.localNode.localConfig.bluetooth.fixed_pin = 654321
        self.radio.localNode.localConfig.security.private_key = b"private-security-key"
        self.service.refresh_diagnostics(self.radio)
        state = self.service.state()
        self.assertEqual(state["device"]["nodeCount"], 121)
        self.assertEqual(len(state["nodes"]), 100)
        self.assertEqual([node["id"] for node in state["nodes"]],
                         [f"!{index:08x}" for index in range(1, 101)])
        peer = state["nodes"][0]
        self.assertEqual(len(peer["name"]), 80)
        self.assertTrue(peer["name"].startswith("<b>"))
        self.assertTrue(peer["viaMqtt"])
        self.assertEqual(peer["hardwareModel"], "HELTEC_V4")
        self.assertIsNone(peer["snr"])
        self.assertIsNone(peer["rssi"])
        self.assertIsNone(peer["hopsAway"])
        self.assertIsNone(peer["latitude"])
        self.assertIsNone(peer["longitude"])
        self.assertEqual(peer["batteryLevel"], 101)
        self.assertEqual(state["diagnostics"]["receivedRadioPackets"], 0)
        self.service.save_snapshot()
        serialized = bridge.encode(state).decode() + "\n".join(self.history.db.iterdump())
        for secret in ("654321", "private-user-key", "private-session", "private-security-key",
                       "adminSession", "publicKey", "private_key", '"online"'):
            self.assertNotIn(secret, serialized)
        self.assertLess(len(bridge.encode(state)), bridge.MAX_STATE_BYTES)

    async def test_routing_diagnostic_only_records_actual_response_not_timeout(self):
        await self.send()
        message_id, _ = self.service.pending[100]
        self.service.pending[100] = (message_id, 0)
        self.service.expire_acks()
        self.assertEqual(self.service.diagnostics["lastRoutingReason"], "")
        self.assertIsNone(self.service.diagnostics["lastRoutingAt"])
        self.radio.queueStatus = types.SimpleNamespace(free=0, maxlen=16)
        self.service.refresh_diagnostics(self.radio)
        self.assertEqual(self.service.diagnostics["queueFree"], 0)
        self.assertEqual(self.service.diagnostics["queueMax"], 16)
        self.service.receive({"decoded": {"portnum": "ROUTING_APP", "requestId": 100,
                                         "routing": {"errorReason": mesh_pb2.Routing.NO_ROUTE}}})
        self.assertEqual(self.service.diagnostics["lastRoutingReason"], "NO_ROUTE")
        self.assertIsInstance(self.service.diagnostics["lastRoutingAt"], int)
        self.assertEqual(self.service.state()["messages"][0]["status"], "unconfirmed")
        self.assertEqual(self.service.state()["messages"][0]["error"], "ack_timeout")

    async def test_connected_ble_liveness_never_checks_mac_as_filesystem_path(self):
        endpoint = "AA:BB:CC:DD:EE:FF"
        connected = asyncio.Event()
        checked = asyncio.Event()
        original_publish = self.service.publish
        original_is_set = self.radio.isConnected.is_set
        def publish():
            original_publish()
            if self.service.connected and self.service.device["transport"] == "ble":
                connected.set()
        def is_connected():
            if connected.is_set():
                checked.set()
            return original_is_set()
        self.radio.close = mock.Mock()
        with mock.patch.dict(bridge.os.environ, {"MESHTASTIC_TRANSPORT": "ble"}), \
                mock.patch.object(bridge, "open_radio", return_value=(self.radio, endpoint)) as opener, \
                mock.patch.object(self.service, "publish", side_effect=publish), \
                mock.patch.object(self.radio.isConnected, "is_set", side_effect=is_connected), \
                mock.patch.object(bridge.Path, "exists", side_effect=AssertionError("MAC is not a path")):
            task = asyncio.create_task(self.service.connect_loop())
            try:
                await asyncio.wait_for(connected.wait(), 2)
                await asyncio.wait_for(checked.wait(), 2)
                self.assertEqual(self.service.device["transport"], "ble")
                self.assertIsInstance(self.service.device["connectedSince"], int)
                self.assertEqual(opener.call_count, 1)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.radio.close.assert_called_once()
        self.assertFalse(self.service.connected)
        self.assertIsNone(self.service.device["connectedSince"])

    async def test_daemon_shutdown_closes_connected_clients_before_waiting_for_server(self):
        path = Path(self.directory.name) / "daemon.sock"
        ready = asyncio.Event()
        handlers = {}
        start_server = asyncio.start_unix_server
        loop = asyncio.get_running_loop()
        async def start(*args, **kwargs):
            server = await start_server(*args, **kwargs)
            ready.set()
            return server
        async def idle(_service):
            await asyncio.Event().wait()
        previous_umask = os.umask(0o077)
        writer = None
        task = None
        try:
            with mock.patch.object(bridge, "socket_path", return_value=path), \
                    mock.patch.dict(bridge.os.environ, {"XDG_DATA_HOME": self.directory.name}), \
                    mock.patch.object(bridge.asyncio, "start_unix_server", side_effect=start), \
                    mock.patch.object(bridge.Bridge, "connect_loop", new=idle), \
                    mock.patch.object(loop, "add_signal_handler",
                                      side_effect=lambda signum, callback: handlers.update({signum: callback})):
                task = asyncio.create_task(bridge.daemon())
                await asyncio.wait_for(ready.wait(), 2)
                reader, writer = await asyncio.open_unix_connection(str(path))
                self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 2))["type"], "state")
                handlers[bridge.signal.SIGTERM]()
                await asyncio.wait_for(task, 2)
                self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        finally:
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            os.umask(previous_umask)


class PureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        language_env = mock.patch.dict(bridge.os.environ,
            {"OMARCHY_LANGUAGE_FILE": str(Path(directory.name) / "missing-language")})
        language_env.start()
        self.addCleanup(language_env.stop)

    def test_explicit_ble_selection_never_opens_usb_or_connects_sdk_twice(self):
        radio = fake_radio()
        radio.connect = mock.Mock()
        radio.waitForConfig = mock.Mock()
        radio.close = mock.Mock()
        radio.client = types.SimpleNamespace(bleak_client=types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF"))
        with mock.patch.dict(bridge.os.environ,
                             {"MESHTASTIC_TRANSPORT": "ble", "MESHTASTIC_BLE_ADDRESS": "AA:BB:CC:DD:EE:FF"}), \
                mock.patch.object(bridge, "discover_port", side_effect=AssertionError("no USB fallback")), \
                mock.patch.object(bridge, "radio_class", return_value=mock.Mock(return_value=radio)) as factory:
            transport = bridge.transport_selection()
            selected, endpoint = bridge.open_radio(transport, mock.Mock())
            self.assertIs(selected, radio)
            self.assertEqual(endpoint, "AA:BB:CC:DD:EE:FF")
            factory.assert_called_once_with("ble")
            radio.connect.assert_not_called()
            radio.waitForConfig.assert_not_called()
        with mock.patch.dict(bridge.os.environ, {"MESHTASTIC_TRANSPORT": "invalid"}):
            with self.assertRaisesRegex(bridge.CommandError, "transport_invalid"):
                bridge.transport_selection()
        with mock.patch.dict(bridge.os.environ, {}, clear=True):
            self.assertEqual(bridge.transport_selection(), "usb")
        radio.isConnected.clear()
        with mock.patch.object(bridge, "radio_class", return_value=mock.Mock(return_value=radio)):
            with self.assertRaisesRegex(bridge.CommandError, "radio_disconnected"):
                bridge.open_radio("ble", mock.Mock())
        radio.close.assert_called_once()

    def test_ble_errors_are_stable_without_leaking_exception_details(self):
        from bleak.exc import BleakDBusError, BleakDeviceNotFoundError
        from meshtastic.ble_interface import BLEInterface
        cases = [
            (BleakDeviceNotFoundError("private address", "secret diagnostic"), "ble_not_found"),
            (BLEInterface.BLEError("secret diagnostic", BLEInterface.BLEError.MULTIPLE_DEVICES), "ble_ambiguous"),
            (BleakDBusError("org.bluez.Error.AuthenticationFailed", ["PIN secret"]), "ble_pairing_required"),
            (BleakDBusError("org.bluez.Error.NotAuthorized", ["secret"]), "ble_pairing_required"),
            (RuntimeError("Bluetooth adapter unavailable secret"), "ble_unavailable"),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(bridge.ble_error(error), expected)
                self.assertNotIn("secret", bridge.ble_error(error))
        wrapped = BLEInterface.BLEError("SDK generic pairing advice", BLEInterface.BLEError.WRITE_ERROR)
        self.assertEqual(bridge.ble_error(wrapped), "ble_unavailable")
        wrapped.__cause__ = BleakDBusError("org.bluez.Error.AuthenticationFailed", ["secret"])
        self.assertEqual(bridge.ble_error(wrapped), "ble_pairing_required")

    def bare_ble(self):
        radio = object.__new__(bridge.radio_class("ble"))
        radio._init_bridge(mock.Mock())
        radio.queueStatus = None
        radio.queue = {}
        radio.client = None
        radio._want_receive = True
        radio.isConnected = threading.Event()
        radio.heartbeatTimer = None
        return radio

    def test_ble_configuration_read_auth_error_survives_constructor_wait(self):
        from bleak.exc import BleakDBusError
        radio = self.bare_ble()
        radio.noProto = False
        radio.should_read = True
        radio.client = types.SimpleNamespace(read_gatt_char=mock.Mock(side_effect=
            BleakDBusError("org.bluez.Error.NotAuthorized", ["private diagnostic"])),
            disconnect=mock.Mock(), close=mock.Mock())
        try:
            radio._receiveFromRadioImpl()
            with self.assertRaisesRegex(bridge.CommandError, "ble_pairing_required"):
                radio._waitConnected(timeout=0)
            self.assertFalse(radio.isConnected.is_set())
            radio._bridge_emit.assert_called_once_with("transport_error", radio, "ble_pairing_required")
        finally:
            radio.close()

    def fake_gatt(self, address):
        return types.SimpleNamespace(address=address, connect=mock.AsyncMock(),
                                     disconnect=mock.AsyncMock(), write_gatt_char=mock.AsyncMock())

    def test_explicit_ble_uses_cached_bluez_device_without_scanning_and_closes_worker(self):
        address = "AA:BB:CC:DD:EE:FF"
        radio = self.bare_ble()
        gatt = self.fake_gatt(address)
        manager = types.SimpleNamespace(get_default_adapter=mock.Mock(return_value="/org/bluez/hci0"),
                                        get_device_address=mock.Mock(return_value=address))
        with mock.patch("meshtastic.ble_interface.BleakClient", return_value=gatt) as client, \
                mock.patch("meshtastic.ble_interface.BleakScanner.discover", new_callable=mock.AsyncMock) as scan, \
                mock.patch("bleak.backends.bluezdbus.manager.get_global_bluez_manager",
                           new=mock.AsyncMock(return_value=manager)):
            try:
                radio.connect(address.lower())
                worker = radio.client._eventThread
                selected = client.call_args.args[0]
                self.assertEqual(selected.address, address)
                self.assertEqual(selected.details["path"], "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF")
                manager.get_device_address.assert_called_once_with(selected.details["path"])
                scan.assert_not_awaited()
                gatt.connect.assert_awaited_once()
                async def never_completes():
                    await asyncio.Event().wait()
                with self.assertRaises(TimeoutError):
                    radio.client.async_await(never_completes(), timeout=0.01)
                async def lost_from_event_loop():
                    radio._bridge_lost(gatt)
                radio.client.async_await(lost_from_event_loop(), timeout=1)
                radio._bridge_emit.assert_called_with("lost", radio)
            finally:
                radio.close()
            self.assertFalse(worker.is_alive())
            self.assertIsNone(radio.client)
            gatt.disconnect.assert_awaited_once()
            radio.close()
            gatt.disconnect.assert_awaited_once()

    def test_missing_cached_ble_address_does_not_scan_or_open_another_device(self):
        from bleak.exc import BleakError
        radio = self.bare_ble()
        manager = types.SimpleNamespace(get_default_adapter=mock.Mock(return_value="/org/bluez/hci0"),
                                        get_device_address=mock.Mock(side_effect=BleakError("unknown device")))
        with mock.patch("meshtastic.ble_interface.BleakClient") as client, \
                mock.patch("meshtastic.ble_interface.BleakScanner.discover", new_callable=mock.AsyncMock) as scan, \
                mock.patch("bleak.backends.bluezdbus.manager.get_global_bluez_manager",
                           new=mock.AsyncMock(return_value=manager)):
            try:
                with self.assertRaisesRegex(bridge.CommandError, "ble_not_found"):
                    radio.connect("AA:BB:CC:DD:EE:FF")
                scan.assert_not_awaited()
                client.assert_not_called()
            finally:
                radio.close()

    def test_unconfigured_ble_scan_requires_one_meshtastic_service_peripheral(self):
        from bleak.backends.device import BLEDevice
        from meshtastic.ble_interface import SERVICE_UUID
        address = "AA:BB:CC:DD:EE:FF"
        one = (BLEDevice(address, "Meshtastic", {}), types.SimpleNamespace(service_uuids=[SERVICE_UUID]))
        other = (BLEDevice("AA:BB:CC:DD:EE:00", "Meshtastic", {}),
                 types.SimpleNamespace(service_uuids=[SERVICE_UUID.upper()]))
        unrelated = (BLEDevice("AA:BB:CC:DD:EE:01", "Meshtastic name only", {}),
                     types.SimpleNamespace(service_uuids=[]))
        for results, expected in [({"x": unrelated}, "ble_not_found"),
                                  ({"one": one, "other": other}, "ble_ambiguous"),
                                  ({"one": one, "x": unrelated}, "")]:
            radio = self.bare_ble()
            gatt = self.fake_gatt(address)
            with self.subTest(expected=expected), \
                    mock.patch("meshtastic.ble_interface.BleakClient", return_value=gatt), \
                    mock.patch("meshtastic.ble_interface.BleakScanner.discover",
                               new=mock.AsyncMock(return_value=results)) as scan:
                try:
                    if expected:
                        with self.assertRaisesRegex(bridge.CommandError, expected):
                            radio.connect(None)
                        gatt.connect.assert_not_awaited()
                    else:
                        radio.connect(None)
                        gatt.connect.assert_awaited_once()
                    scan.assert_awaited_once_with(timeout=10, return_adv=True, service_uuids=[SERVICE_UUID])
                finally:
                    radio.close()

    def test_ble_constructor_failure_cleans_up_sdk_client_for_all_error_types(self):
        from meshtastic.ble_interface import BLEInterface
        radio_class = bridge.radio_class("ble")
        client = types.SimpleNamespace(disconnect=mock.Mock(), close=mock.Mock())
        def failed_constructor(radio, **_kwargs):
            radio.client = client
            raise RuntimeError("arbitrary SDK failure after connecting")
        with mock.patch.object(BLEInterface, "__init__", new=failed_constructor):
            with self.assertRaisesRegex(RuntimeError, "arbitrary SDK failure"):
                radio_class("AA:BB:CC:DD:EE:FF", mock.Mock())
        client.disconnect.assert_called_once()
        client.close.assert_called_once()

    def test_both_transports_share_bounded_send_without_replay_after_queue_rejection(self):
        for transport in ("usb", "ble"):
            radio = (bridge.radio_class()("/dev/never-opened", mock.Mock())
                     if transport == "usb" else self.bare_ble())
            radio.isConnected.set()
            outgoing = mesh_pb2.ToRadio()
            outgoing.packet.id = 42
            radio._sendToRadioImpl = mock.Mock(side_effect=OSError("failed local write"))
            try:
                with self.assertRaises(OSError):
                    radio._sendToRadio(outgoing)
                self.assertEqual(len(getattr(radio, "queue", {})), 0)
                radio._sendToRadioImpl.assert_called_once()
                radio.queueStatus = types.SimpleNamespace(free=0)
                with mock.patch.object(bridge.time, "monotonic", side_effect=(0, 4)):
                    with self.assertRaisesRegex(bridge.CommandError, "backend_busy"):
                        radio._sendToRadio(outgoing)
                self.assertEqual(radio._sendToRadioImpl.call_count, 1)
                radio.isConnected.clear()
                with self.assertRaisesRegex(bridge.CommandError, "radio_disconnected"):
                    radio._sendToRadio(outgoing)
            finally:
                radio.close()

    def test_serial_config_completion_after_close_cannot_restart_heartbeat(self):
        radio = bridge.radio_class()("/dev/never-opened", mock.Mock())
        radio.close()
        radio._sendToRadioImpl = mock.Mock()
        radio._connected()
        radio.sendHeartbeat()
        self.assertFalse(radio.isConnected.is_set())
        self.assertIsNone(radio.heartbeatTimer)
        radio._sendToRadioImpl.assert_not_called()

    def test_notification_summary_and_body_are_escaped_and_not_shell_commands(self):
        args = bridge.notification_args('<img src="x"> & sender', "<channel>", '<b>hi</b> & $(touch /tmp/no)')
        self.assertEqual(args[0], "notify-send")
        self.assertEqual(args[-3], "--")
        self.assertNotIn("<img", args[-2])
        self.assertIn("&lt;img", args[-2])
        self.assertEqual(args[-1], "&lt;b&gt;hi&lt;/b&gt; &amp; $(touch /tmp/no)")
        self.assertIn("--action=default=Open Meshtastic", args)

    def test_bare_key_accepts_only_supported_base64_lengths_and_public_key_range(self):
        for key in [bytes([value]) for value in range(11)] + [b"\xfb\xff" * 8, b"\xff" * 32]:
            for encoder in (base64.b64encode, base64.urlsafe_b64encode):
                encoded = encoder(key).decode()
                for value in (encoded, encoded.rstrip("=")):
                    with self.subTest(value=value):
                        self.assertEqual(bridge.decode_key(value, "ą" * 5), key)
        for key in (b"", b"\x0b", b"\xff", b"x" * 2, b"x" * 15, b"x" * 17, b"x" * 31, b"x" * 33):
            with self.subTest(key=key), self.assertRaisesRegex(bridge.CommandError, "^invalid_key$"):
                bridge.decode_key(base64.b64encode(key).decode(), "Name")
        for value in ("0x0123456789abcdef", "password", "AA AA", "AQ===", "=AQ", "A", "AQ==\n"):
            with self.subTest(value=value), self.assertRaisesRegex(bridge.CommandError, "^invalid_key$"):
                bridge.decode_key(value, "Name")

    def test_single_channel_writer_revalidates_after_admin_session_before_transaction(self):
        radio = fake_radio()
        node = radio.localNode
        target = channel(2, "New", b"k" * 32)
        node.beginSettingsTransaction = mock.Mock()
        node.writeChannel = mock.Mock()
        node.writeConfig = mock.Mock()
        node.commitSettingsTransaction = mock.Mock()
        def refresh_channels():
            node.channels.append(channel(2, "Occupied"))
            radio.nodesByNum[radio.myInfo.my_node_num]["adminSessionPassKey"] = b"session"
        node.ensureSessionKey = mock.Mock(side_effect=refresh_channels)
        with self.assertRaisesRegex(bridge.CommandError, "^preview_expired$"):
            bridge.write_channel(radio, target, None)
        node.ensureSessionKey.assert_called_once_with()
        node.beginSettingsTransaction.assert_not_called()
        node.writeChannel.assert_not_called()
        node.writeConfig.assert_not_called()
        node.commitSettingsTransaction.assert_not_called()

    def test_single_channel_writer_waits_for_admin_session_without_mutating_settings(self):
        radio = fake_radio()
        node = radio.localNode
        node.ensureSessionKey = mock.Mock()
        node.beginSettingsTransaction = mock.Mock()
        node.writeChannel = mock.Mock()
        before = [row.SerializeToString() for row in node.channels]
        with mock.patch.object(bridge.time, "monotonic", side_effect=(0, 6)):
            with self.assertRaisesRegex(bridge.CommandError, "^config_failed$"):
                bridge.write_channel(radio, channel(2, "New", b"k" * 32), None)
        node.ensureSessionKey.assert_called_once_with()
        node.beginSettingsTransaction.assert_not_called()
        node.writeChannel.assert_not_called()
        self.assertEqual([row.SerializeToString() for row in node.channels], before)

    def test_strict_url_validation(self):
        for url in ("http://meshtastic.org/d/#AAAA", "https://evil.example/d/#AAAA",
                    "https://meshtastic.org.evil/d/#AAAA", "https://meshtastic.org/d/#%%%",
                    "https://user@meshtastic.org/d/#AAAA", "https://meshtastic.org:443/d/#AAAA",
                    "https://meshtastic.org/d/?add=true#AAAA", "https://[/d/#AAAA", None):
            with self.subTest(url=url), self.assertRaises(bridge.CommandError):
                bridge.decode_config(url)

    def test_invalid_config_keys_channels_regions_and_override_are_rejected(self):
        current = fake_radio().localNode.localConfig.lora
        configurations = []
        for key in (b"xx", b"x" * 15, b"\xff"):
            config = bridge.berlin_config()
            config.settings[0].psk = key
            configurations.append(config)
        configurations.append(apponly_pb2.ChannelSet())
        config = bridge.berlin_config()
        for _ in range(7):
            config.settings.add(name="Extra", psk=b"\x01")
        configurations.append(config)
        config = bridge.berlin_config()
        config.lora_config.region = 1
        configurations.append(config)
        config = bridge.berlin_config()
        config.lora_config.override_frequency = 900
        configurations.append(config)
        config = bridge.berlin_config()
        config.lora_config.override_duty_cycle = True
        configurations.append(config)
        for config in configurations:
            with self.assertRaises(bridge.CommandError):
                bridge.effective_config(config, current)

    def test_longfast_zero_preset_replaces_nonzero_and_region_is_preserved(self):
        current = fake_radio().localNode.localConfig.lora
        current.modem_preset = 4
        config = apponly_pb2.ChannelSet(settings=[channel().settings])
        config.lora_config.use_preset = True
        channels, lora, warnings = bridge.effective_config(config, current)
        self.assertEqual(lora.modem_preset, 0)
        self.assertEqual(lora.region, 3)
        self.assertTrue(lora.tx_enabled)
        self.assertEqual(bridge.describe_channel(channels[0], lora)["name"], "LongFast")
        self.assertIn("region_preserved", warnings)

    def test_configuration_clears_all_removed_channels_in_one_transaction(self):
        current = fake_radio().localNode.localConfig
        node = types.SimpleNamespace(localConfig=current, channels=[],
            beginSettingsTransaction=mock.Mock(), writeChannel=mock.Mock(),
            writeConfig=mock.Mock(), commitSettingsTransaction=mock.Mock())
        channels, lora, _ = bridge.effective_config(bridge.berlin_config(), current.lora, berlin=True)
        interface = types.SimpleNamespace(localNode=node, nodesByNum={1: {"adminSessionPassKey": b"fake"}},
                                          myInfo=types.SimpleNamespace(my_node_num=1))
        node.ensureSessionKey = mock.Mock()
        bridge.write_config(interface, channels, lora)
        node.beginSettingsTransaction.assert_called_once_with()
        self.assertEqual(node.writeChannel.call_args_list, [mock.call(index) for index in range(8)])
        self.assertEqual([ch.role for ch in node.channels], [1, 2, 0, 0, 0, 0, 0, 0])
        self.assertEqual([ch.index for ch in node.channels], list(range(8)))
        node.writeConfig.assert_called_once_with("lora")
        node.commitSettingsTransaction.assert_called_once_with()

    def test_serial_discovery_does_not_grab_generic_or_ambiguous_ports(self):
        generic = types.SimpleNamespace(device="/dev/ttyUSB0", description="CP2102 USB UART",
                                        manufacturer="Silicon Labs", product="USB Serial")
        radio = types.SimpleNamespace(device="/dev/ttyACM0", description="Heltec Meshtastic",
                                      manufacturer="Heltec", product="Radio")
        with mock.patch.dict(bridge.os.environ, {}, clear=True):
            with mock.patch("serial.tools.list_ports.comports", return_value=[generic]):
                with self.assertRaisesRegex(bridge.CommandError, "radio_unavailable"):
                    bridge.discover_port()
            with mock.patch("serial.tools.list_ports.comports", return_value=[radio, types.SimpleNamespace(
                    device="/dev/ttyACM1", description="Meshtastic", manufacturer="", product="")]):
                with self.assertRaisesRegex(bridge.CommandError, "radio_ambiguous"):
                    bridge.discover_port()

    def test_sdk_transport_never_retains_or_retries_a_failed_write(self):
        from meshtastic.protobuf import mesh_pb2
        radio = bridge.radio_class()("/dev/never-opened", mock.Mock())
        radio.isConnected.set()
        packet = mesh_pb2.ToRadio()
        packet.packet.id = 42
        radio._sendToRadioImpl = mock.Mock(side_effect=OSError("write failed"))
        with self.assertRaises(OSError):
            radio._sendToRadio(packet)
        self.assertEqual(len(radio.queue), 0)
        radio._sendToRadioImpl.assert_called_once_with(packet)
        radio.close()

    def test_sdk_transport_wait_for_full_queue_is_bounded(self):
        from meshtastic.protobuf import mesh_pb2
        radio = bridge.radio_class()("/dev/never-opened", mock.Mock())
        radio.isConnected.set()
        radio.queueStatus = types.SimpleNamespace(free=0)
        packet = mesh_pb2.ToRadio()
        packet.packet.id = 42
        radio._sendToRadioImpl = mock.Mock()
        with mock.patch.object(bridge.time, "monotonic", side_effect=(0, 4)):
            with self.assertRaisesRegex(bridge.CommandError, "backend_busy"):
                radio._sendToRadio(packet)
        radio._sendToRadioImpl.assert_not_called()
        radio.close()

    def test_restarted_queued_messages_become_unconfirmed_without_resending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private/history.sqlite3"
            history = bridge.History(path)
            history.add(radio="!1", profile="profile", channel=0, sender_num=1, packet_id=3,
                        sender="me", text="old", timestamp=1, outgoing=True, status="queued")
            history.db.close()
            reopened = bridge.History(path)
            try:
                self.assertEqual(reopened.messages("profile")[0]["status"], "unconfirmed")
                self.assertEqual(reopened.messages("profile")[0]["error"], "ack_timeout")
            finally:
                reopened.db.close()


if __name__ == "__main__":
    unittest.main()
