#!/usr/bin/env python3
"""Private Meshtastic history service and JSONL Unix-socket client.

Only the daemon imports/opens the radio SDK. No configuration is written except
by a one-shot, explicitly confirmed settings preview. Radio routing ACKs are not
recipient reads. The Python SDK queue is deliberately replaced with one bounded
local write: failed requests are never replayed by a later heartbeat.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
from dataclasses import dataclass
import fcntl
import hashlib
import html
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import stat
import struct
import sys
import threading
import time
import unicodedata
from urllib.parse import urlsplit

LOG = logging.getLogger("omarchy-meshtastic")
MAX_TEXT_BYTES = 228
MAX_COMMAND_BYTES = 16384
MAX_STATE_BYTES = 4 * 1024 * 1024
ACK_TIMEOUT = 90
PREVIEW_TTL = 300
BROADCAST = 0xFFFFFFFF
MAX_NODES = 100


class CommandError(Exception):
    pass


def clean_text(value, limit=256):
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value[:limit] if c in "\n\t" or ord(c) >= 32).replace("\x7f", "")


def integer(value, default=-1):
    return value if type(value) is int else default


def number(value, low, high):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        return None
    return value


def mapping(value):
    return value if isinstance(value, dict) else {}


def diagnostic_defaults():
    return {"updatedAt": None, "lastRadioRxAt": None, "lastRadioRxFrom": "",
            "lastRadioRxSnr": None, "lastRadioRxRssi": None, "receivedRadioPackets": 0,
            "heardSinceConnect": 0, "queueFree": None, "queueMax": None,
            "lastRoutingReason": "", "lastRoutingAt": None}


def device_defaults():
    return {"name": "", "id": "", "firmware": "", "port": "", "transport": "usb",
            "endpoint": "", "connectedSince": None, "region": 0, "regionName": "UNSET",
            "nodeCount": 0, "modemPreset": "", "txEnabled": False, "hardwareModel": "",
            "role": "", "frequencySlot": None, "frequencyMHz": None, "hopLimit": None,
            "txPower": None, "batteryLevel": None, "voltage": None, "uptimeSeconds": None,
            "channelUtilization": None, "airUtilTx": None, "bluetoothEnabled": None}


def enum_name(enum, value, fallback="UNKNOWN"):
    try:
        return enum.Name(value)
    except ValueError:
        return fallback


def preset_name(value):
    from meshtastic.protobuf import config_pb2
    return "".join(word.title() for word in enum_name(
        config_pb2.Config.LoRaConfig.ModemPreset, value).split("_"))


def channel_identity(radio, channel, lora):
    settings = channel.settings
    public = json.dumps([radio, channel.index, settings.name, settings.id, lora.region,
                         lora.use_preset, lora.modem_preset, lora.bandwidth, lora.spread_factor,
                         lora.coding_rate, lora.channel_num, lora.override_frequency,
                         lora.frequency_offset], ensure_ascii=True)
    # A one-way channel namespace, never the radio key or shared URL itself.
    return hashlib.sha256(public.encode() + b"\0" + bytes(settings.psk)).hexdigest()


def describe_channel(channel, lora):
    return {"index": channel.index,
            "name": clean_text(channel.settings.name, 64) or preset_name(lora.modem_preset),
            "primary": channel.role == 1, "role": "PRIMARY" if channel.role == 1 else "SECONDARY",
            "encrypted": any(channel.settings.psk)}


def notification_label():
    """Resolve the current native language at delivery, independently of the GUI."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    language_file = os.environ.get("OMARCHY_LANGUAGE_FILE") or str(Path(config_home) / "omarchy/language")
    try:
        language = Path(language_file).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        language = "en"
    if not re.fullmatch(r"[a-z]{2,3}(-[A-Za-z]{2})?", language):
        language = "en"
    for code in dict.fromkeys((language, language.split("-")[0], "en")):
        try:
            catalog = json.loads((Path(__file__).resolve().parent / "i18n" / f"{code}.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        label = catalog.get("notifications.open") if isinstance(catalog, dict) else None
        if isinstance(label, str) and label:
            return label
    return "Open Meshtastic"


def notification_args(sender, channel, text):
    summary = html.escape(f"{clean_text(sender, 80)} · {clean_text(channel, 64)}", quote=True)
    body = html.escape(clean_text(text, MAX_TEXT_BYTES), quote=True)
    return ["notify-send", "--app-name=Meshtastic", "--category=im.received",
            "--expire-time=10000", "--wait", f"--action=default={notification_label()}", "--", summary, body]


def discover_port():
    from serial.tools import list_ports
    override = os.environ.get("MESHTASTIC_PORT")
    if override:
        if not os.path.isabs(override) or not Path(override).exists():
            raise CommandError("radio_unavailable")
        return override
    candidates = []
    for port in list_ports.comports():
        description = " ".join(str(getattr(port, field, "") or "")
                               for field in ("description", "manufacturer", "product"))
        if re.search(r"meshtastic|heltec|lilygo|t-beam|t-echo|wisblock|rak wireless|wio tracker",
                     description, re.IGNORECASE):
            candidates.append(port.device)
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) != 1:
        raise CommandError("radio_ambiguous" if candidates else "radio_unavailable")
    device = candidates[0]
    for stable in sorted(Path("/dev/serial/by-id").glob("*")):
        if os.path.realpath(stable) == os.path.realpath(device):
            return str(stable)
    return device


class BoundedSend:
    """Replace the SDK replay queue with a single bounded local write."""

    def _init_bridge(self, emit):
        self._bridge_io = threading.RLock()
        self._bridge_emit = emit
        self._bridge_closed = False

    def _connected(self):
        with self._bridge_io:
            if not self._bridge_closed:
                super()._connected()

    def sendHeartbeat(self):
        with self._bridge_io:
            if self._bridge_closed:
                if self.heartbeatTimer:
                    self.heartbeatTimer.cancel()
                    self.heartbeatTimer = None
                return
            super().sendHeartbeat()

    def _sendToRadio(self, packet):
        with self._bridge_io:
            if packet.HasField("packet"):
                if not self.isConnected.is_set():
                    raise CommandError("radio_disconnected")
                deadline = time.monotonic() + 3
                while self.queueStatus is not None and self.queueStatus.free <= 0:
                    if not self.isConnected.is_set():
                        raise CommandError("radio_disconnected")
                    if time.monotonic() >= deadline:
                        raise CommandError("backend_busy")
                    time.sleep(0.05)
                self._queueClaim()
            self._sendToRadioImpl(packet)

    def _handleQueueStatusFromRadio(self, status):
        self.queueStatus = status
        self._bridge_emit("diagnostics", self)
        if status.res and status.mesh_packet_id:
            self._bridge_emit("rejected", self, status.mesh_packet_id)


def radio_class(transport="usb"):
    if transport == "ble":
        return ble_radio_class()
    if transport != "usb":
        raise CommandError("transport_invalid")
    from meshtastic.serial_interface import SerialInterface
    import serial

    class BoundedSerialInterface(BoundedSend, SerialInterface):
        def __init__(self, port, emit):
            self._init_bridge(emit)
            super().__init__(devPath=port, connectNow=False, timeout=15)

        def _writeBytes(self, data):
            with self._bridge_io:
                if self.stream is None or not self.stream.is_open:
                    raise serial.SerialException("Radio stream closed")
                self.stream.write_timeout = 3
                if self.stream.write(data) != len(data):
                    raise serial.SerialTimeoutException("Incomplete serial write")
                time.sleep(0.1)


        def close(self):
            # SerialInterface.close() calls unbounded tcdrain/flush; closing the
            # handle directly also wakes its reader without another radio write.
            with self._bridge_io:
                self._bridge_closed = True
                self._wantExit = True
                if self.heartbeatTimer:
                    self.heartbeatTimer.cancel()
                    self.heartbeatTimer = None
                stream, self.stream = self.stream, None
                if stream is not None:
                    stream.close()
            if self._rxThread.is_alive() and self._rxThread is not threading.current_thread():
                self._rxThread.join(timeout=2)
            self.isConnected.clear()

    return BoundedSerialInterface


def ble_error(error):
    """Map SDK/Bleak failures without exposing exception text or credentials."""
    if isinstance(error, CommandError):
        return str(error)
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        kind = getattr(error, "kind", "")
        if kind == "device_not_found" or type(error).__name__ == "BleakDeviceNotFoundError":
            return "ble_not_found"
        if kind == "multiple_devices":
            return "ble_ambiguous"
        detail = (str(getattr(error, "dbus_error", "")) + " " + str(error)).lower()
        if any(word in detail for word in (
                "authentication", "notauthorized", "not authorized", "insufficient authorization",
                "insufficient encryption", "not paired", "pairing", "pin or key missing",
                "att error: 0x05", "att error: 0x0f")) and kind != "write_error":
            return "ble_pairing_required"
        error = error.__cause__ or error.__context__
    return "ble_unavailable"


def ble_radio_class():
    from meshtastic.ble_interface import BLEInterface, BLEClient, SERVICE_UUID, FROMRADIO_UUID, TORADIO_UUID

    class BoundedBLEClient(BLEClient):
        def async_await(self, coro, timeout=None):
            future = self.async_run(coro)
            try:
                return future.result(15 if timeout is None else timeout)
            except TimeoutError:
                future.cancel()
                raise

        def close(self):
            # The SDK joins forever and its disconnect callback closes from the
            # very loop it must wait on. Our callback only reports loss instead.
            if not self._eventThread.is_alive():
                return
            async def stop():
                tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                self.async_await(stop(), timeout=2)
            self._eventLoop.call_soon_threadsafe(self._eventLoop.stop)
            if self._eventThread is not threading.current_thread():
                self._eventThread.join(timeout=2)
            if self._eventThread.is_alive():
                raise RuntimeError("Bluetooth worker did not stop")

    class BoundedBLEInterface(BoundedSend, BLEInterface):
        def __init__(self, address, emit):
            self._init_bridge(emit)
            self.client = None
            try:
                # Unlike SerialInterface this constructor connects and reads
                # configuration itself; never call connect() again afterward.
                super().__init__(address=address, timeout=15)
            except BaseException:
                self.close()
                raise

        def connect(self, address=None):
            if address and not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address):
                raise CommandError("ble_not_found")
            with BoundedBLEClient() as scanner:
                if address:
                    # A parent-selected/paired BlueZ device is already known.
                    # Passing its real BLEDevice avoids an implicit scan.
                    from bleak.backends.bluezdbus.manager import get_global_bluez_manager
                    from bleak.backends.device import BLEDevice
                    async def cached_device():
                        manager = await get_global_bluez_manager()
                        path = manager.get_default_adapter() + "/dev_" + address.upper().replace(":", "_")
                        try:
                            cached_address = manager.get_device_address(path)
                        except Exception:
                            raise CommandError("ble_not_found") from None
                        return BLEDevice(cached_address, None, {"path": path})
                    device = scanner.async_await(cached_device())
                else:
                    response = scanner.discover(timeout=10, return_adv=True, service_uuids=[SERVICE_UUID])
                    candidates = {}
                    for device, advertisement in response.values():
                        if SERVICE_UUID in [uuid.lower() for uuid in advertisement.service_uuids]:
                            candidates[device.address.lower()] = device
                    if len(candidates) != 1:
                        raise CommandError("ble_ambiguous" if candidates else "ble_not_found")
                    device = next(iter(candidates.values()))
            self.client = BoundedBLEClient(device, disconnected_callback=self._bridge_lost)
            self.client.connect()
            # Bleak.connect() already resolves services. The SDK's subsequent
            # client.discover() is another scanner pass, not service discovery.
            return self.client

        def _waitConnected(self, timeout=15):
            try:
                return super()._waitConnected(timeout=min(timeout, 15))
            except Exception:
                if self.failure:
                    raise self.failure from None
                raise

        def _bridge_lost(self, _client):
            self._want_receive = False
            self.isConnected.clear()
            self._bridge_emit("lost", self)

        def _receiveFromRadioImpl(self):
            try:
                while self._want_receive:
                    if not self.should_read or self.client is None:
                        time.sleep(0.01)
                        continue
                    self.should_read = False
                    empty = 0
                    while self._want_receive:
                        data = bytes(self.client.read_gatt_char(FROMRADIO_UUID))
                        if data:
                            empty = 0
                            self._handleFromRadio(data)
                        elif empty < 5:
                            empty += 1
                            time.sleep(0.1)
                        else:
                            break
            except Exception as error:
                if not self._bridge_closed:
                    self.failure = CommandError(ble_error(error))
                    self._bridge_emit("transport_error", self, str(self.failure))
                self.isConnected.clear()
                self._want_receive = False

        def _sendToRadioImpl(self, packet):
            if self.client is None or self._bridge_closed:
                raise CommandError("radio_disconnected")
            data = packet.SerializeToString()
            if data:
                try:
                    self.client.write_gatt_char(TORADIO_UUID, data, response=True)
                except Exception as error:
                    self._bridge_emit("transport_error", self, ble_error(error))
                    raise
                self.should_read = True

        def close(self):
            with self._bridge_io:
                if self._bridge_closed:
                    return
                self._bridge_closed = True
                self._want_receive = False
                if getattr(self, "heartbeatTimer", None):
                    self.heartbeatTimer.cancel()
                    self.heartbeatTimer = None
                client, self.client = self.client, None
            if client is not None:
                import atexit
                if hasattr(self, "_exit_handler"):
                    atexit.unregister(self._exit_handler)
                # Do not send a mesh disconnect or touch RF/configuration.
                try:
                    with contextlib.suppress(Exception):
                        client.disconnect()
                finally:
                    client.close()
            reader = getattr(self, "_receiveThread", None)
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=2)
                if reader.is_alive():
                    raise RuntimeError("Bluetooth receiver did not stop")
            if hasattr(self, "isConnected"):
                self.isConnected.clear()

    return BoundedBLEInterface


def transport_selection():
    transport = os.environ.get("MESHTASTIC_TRANSPORT", "usb")
    if transport not in ("usb", "ble"):
        raise CommandError("transport_invalid")
    return transport


def open_radio(transport, emit):
    """Run the entire blocking SDK connection on the daemon's worker."""
    endpoint = discover_port() if transport == "usb" else os.environ.get("MESHTASTIC_BLE_ADDRESS") or None
    interface = radio_class(transport)(endpoint, emit)
    try:
        if transport == "usb":
            interface.connect()
            interface.waitForConfig()
        else:
            endpoint = interface.client.bleak_client.address
        if not interface.isConnected.is_set():
            raise CommandError("radio_disconnected")
        return interface, endpoint
    except BaseException:
        interface.close()
        raise


class History:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.is_symlink():
            raise RuntimeError("History path must not be a symlink")
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, radio TEXT NOT NULL, profile TEXT NOT NULL,
                channel INTEGER NOT NULL, sender_num INTEGER NOT NULL, packet_id INTEGER,
                sender TEXT NOT NULL, text TEXT NOT NULL, timestamp INTEGER NOT NULL,
                outgoing INTEGER NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                unread INTEGER NOT NULL DEFAULT 0,
                UNIQUE(radio, sender_num, packet_id));
            CREATE INDEX IF NOT EXISTS channel_history ON messages(profile, id);
        """)
        self.db.execute("UPDATE messages SET status='unconfirmed',error='ack_timeout' WHERE status='queued'")
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=True)))
        self.db.commit()

    def add(self, *, radio, profile, channel, sender_num, packet_id, sender, text, timestamp,
            outgoing, status, error="", unread=False):
        cursor = self.db.execute("""INSERT OR IGNORE INTO messages
            (radio,profile,channel,sender_num,packet_id,sender,text,timestamp,outgoing,status,error,unread)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (radio, profile, channel, sender_num, packet_id, sender, text, timestamp,
             int(outgoing), status, error, int(unread)))
        self.db.commit()
        return cursor.lastrowid if cursor.rowcount else None

    def update(self, message_id, status, error="", packet_id=None):
        if packet_id is None:
            self.db.execute("UPDATE messages SET status=?,error=? WHERE id=?", (status, error, message_id))
        else:
            self.db.execute("UPDATE messages SET status=?,error=?,packet_id=? WHERE id=?",
                            (status, error, packet_id, message_id))
        self.db.commit()

    def read(self, profile):
        self.db.execute("UPDATE messages SET unread=0 WHERE profile=? AND unread=1", (profile,))
        self.db.commit()

    def messages(self, profile):
        rows = self.db.execute("""SELECT id,channel,sender,text,timestamp,outgoing,status,error
            FROM messages WHERE profile=? ORDER BY id DESC LIMIT 200""", (profile,)).fetchall()
        return [dict(row, outgoing=bool(row["outgoing"])) for row in reversed(rows)]

    def unread(self, profile):
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE profile=? AND unread=1", (profile,)).fetchone()[0]


class Notifications:
    def __init__(self, enabled):
        self.enabled = enabled
        self.queue = asyncio.Queue(maxsize=16)

    def enqueue(self, sender, channel, text):
        if self.enabled():
            try:
                self.queue.put_nowait((time.monotonic(), clean_text(sender, 80),
                                       clean_text(channel, 64), clean_text(text, MAX_TEXT_BYTES)))
            except asyncio.QueueFull:
                LOG.warning("Notification queue full; omitting toast")

    async def run(self):
        while True:
            created, sender, channel, text = await self.queue.get()
            if not self.enabled() or time.monotonic() - created > 15:
                continue
            process = None
            try:
                process = await asyncio.create_subprocess_exec(*notification_args(sender, channel, text), stdout=asyncio.subprocess.PIPE,
                                                               stderr=asyncio.subprocess.DEVNULL)
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=12)
                if stdout.strip() == b"default":
                    opener = await asyncio.create_subprocess_exec("omarchy-shell", "shell", "summon",
                        "miszarchy.meshtastic", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    try:
                        await asyncio.wait_for(opener.wait(), 5)
                    except asyncio.TimeoutError:
                        opener.kill()
                        await opener.wait()
            except (OSError, asyncio.TimeoutError):
                LOG.warning("Desktop notification unavailable or expired")
            finally:
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()


def decode_config(url):
    from google.protobuf.message import DecodeError
    from meshtastic.protobuf import apponly_pb2
    if not isinstance(url, str) or len(url) > 8192:
        raise CommandError("invalid_url")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "meshtastic.org" or parsed.path != "/d/"
                or parsed.query or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", parsed.fragment)):
            raise CommandError("invalid_url")
        payload = base64.b64decode(parsed.fragment + "=" * (-len(parsed.fragment) % 4), altchars=b"-_", validate=True)
        config = apponly_pb2.ChannelSet()
        config.ParseFromString(payload)
    except (ValueError, DecodeError):
        # Do not include decoder diagnostics: they can contain key-bearing input.
        raise CommandError("invalid_url") from None
    known = apponly_pb2.ChannelSet()
    known.CopyFrom(config)
    known.DiscardUnknownFields()
    if config.SerializeToString(deterministic=True) != known.SerializeToString(deterministic=True):
        raise CommandError("invalid_config")
    return config


def berlin_config():
    from meshtastic.protobuf import apponly_pb2, config_pb2
    config = apponly_pb2.ChannelSet()
    for name in ("", "TEST"):
        channel = config.settings.add(name=name, psk=b"\x01")
        channel.module_settings.position_precision = 15
    lora = config.lora_config
    lora.use_preset = True
    lora.modem_preset = config_pb2.Config.LoRaConfig.MEDIUM_FAST
    lora.region = config_pb2.Config.LoRaConfig.EU_868
    lora.tx_enabled = True
    lora.channel_num = 1
    return config


def effective_config(config, current, berlin=False):
    from meshtastic.protobuf import channel_pb2, config_pb2
    if not 1 <= len(config.settings) <= 8:
        raise CommandError("invalid_config")
    for channel in config.settings:
        if (len(channel.name.encode("utf-8")) > 11 or clean_text(channel.name) != channel.name
                or len(channel.psk) not in (0, 1, 16, 32)
                or (len(channel.psk) == 1 and channel.psk[0] > 10)
                or channel.module_settings.position_precision > 32):
            raise CommandError("invalid_config")
    lora = config_pb2.Config.LoRaConfig()
    lora.CopyFrom(current)
    lora.MergeFrom(config.lora_config)
    if config.HasField("lora_config"):
        if not config.lora_config.use_preset:
            raise CommandError("invalid_config")
        # Zero is meaningful for LongFast and automatic frequency selection;
        # MergeFrom would otherwise retain the old nonzero scalar values.
        lora.modem_preset = config.lora_config.modem_preset
        lora.channel_num = config.lora_config.channel_num
    if not config.lora_config.region:
        lora.region = current.region
    if berlin:
        lora.override_frequency = 0
        lora.frequency_offset = 0
        lora.channel_num = 1
    # RegionInfo spans/profiles and preset bandwidths from firmware
    # src/mesh/RadioInterface.cpp: reject profiles the firmware would clamp or
    # silently swap to another region. 0 is automatic slot selection.
    regional_slots = {
        2: {0: 4, 1: 8, 3: 4, 4: 4, 5: 4, 6: 4, 7: 8, 8: 2, 9: 2},
        3: {0: 1, 1: 2, 3: 1, 4: 1, 5: 1, 6: 1, 7: 2},
        29: {10: 4, 11: 4},
        32: {12: 3, 13: 3},
    }
    slot_limit = regional_slots.get(lora.region, {}).get(lora.modem_preset)
    if not lora.region and lora.modem_preset in config_pb2.Config.LoRaConfig.ModemPreset.values():
        slot_limit = 8  # Preview only; applying an unset region is forbidden.
    if (slot_limit is None or not lora.use_preset or lora.channel_num > slot_limit
            or lora.hop_limit > 7 or lora.override_frequency or lora.frequency_offset
            or config.lora_config.override_duty_cycle):
        raise CommandError("invalid_config")
    channels = []
    for index, settings in enumerate(config.settings):
        channel = channel_pb2.Channel(index=index, role=1 if index == 0 else 2, settings=settings)
        channels.append(channel)
    warnings = ["replaces_channels"]
    if not config.lora_config.region:
        warnings.append("region_preserved")
    if not lora.region:
        warnings.append("region_unset")
    if any(channel.settings.uplink_enabled for channel in channels):
        warnings.append("uplink_enabled")
    if any(channel.settings.downlink_enabled for channel in channels):
        warnings.append("downlink_enabled")
    if any(len(channel.settings.psk) <= 1 for channel in channels):
        warnings.append("public_key")
    return channels, lora, warnings


@dataclass(repr=False)
class ConfigPreview:
    token: str
    expires_at: float
    generation: int
    radio_id: str
    channels: list
    lora: object
    operation: str = "replace"
    target_name: str = ""
    target_index: int | None = None
    expected_target: bytes | None = None


def decode_key(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9+/_-]+={0,2}", value):
        raise CommandError("invalid_key")
    try:
        key = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise CommandError("invalid_key") from None
    if len(key) not in (1, 16, 32) or (len(key) == 1 and key[0] > 10):
        raise CommandError("invalid_key")
    try:
        valid_name = (isinstance(name, str) and bool(name.strip())
                      and len(name.encode("utf-8")) <= 11
                      and not any(unicodedata.category(char) == "Cc" for char in name))
    except UnicodeError:
        valid_name = False
    if not valid_name:
        raise CommandError("invalid_channel_name")
    return key


def channel_slot(node, index):
    slots = [channel for channel in node.channels or [] if channel.index == index]
    if len(slots) > 1:
        raise CommandError("preview_expired")
    return slots[0] if slots else None


def key_config(interface, key, name):
    from meshtastic.protobuf import channel_pb2, config_pb2
    lora = config_pb2.Config.LoRaConfig()
    lora.CopyFrom(interface.localNode.localConfig.lora)
    channels = []
    for raw in interface.localNode.channels or []:
        if raw.role in (1, 2):
            channel = channel_pb2.Channel()
            channel.CopyFrom(raw)
            channels.append(channel)
    matches = [channel for channel in channels if describe_channel(channel, lora)["name"] == name]
    if len(matches) > 1:
        raise CommandError("channel_ambiguous")
    if matches:
        target = matches[0]
        operation = "update"
    else:
        occupied = {channel.index for channel in channels}
        index = next((index for index in range(1, 8) if index not in occupied), None)
        if index is None:
            raise CommandError("channels_full")
        target = channel_pb2.Channel(index=index, role=2)
        target.settings.name = name
        channels.append(target)
        operation = "add"
    original = channel_slot(interface.localNode, target.index)
    expected = original.SerializeToString(deterministic=True) if original is not None else None
    target.settings.psk = key
    channels.sort(key=lambda channel: channel.index)
    warnings = ["other_channels_preserved"]
    if len(key) == 1:
        warnings.append("public_key")
    if target.settings.uplink_enabled:
        warnings.append("uplink_enabled")
    if target.settings.downlink_enabled:
        warnings.append("downlink_enabled")
    if not lora.region:
        warnings.append("region_unset")
    if name == "BerlinMesh" and (lora.region != config_pb2.Config.LoRaConfig.EU_868
            or not lora.use_preset or lora.modem_preset != config_pb2.Config.LoRaConfig.MEDIUM_FAST
            or lora.channel_num != 1 or lora.override_frequency or lora.frequency_offset):
        warnings.append("berlin_profile_mismatch")
    return channels, lora, warnings, operation, target.index, expected


def ensure_admin_session(interface):
    node = interface.localNode
    # ensureSessionKey requests but does not wait in SDK 2.7.11. Resolve that
    # read-only prerequisite before beginning any settings mutation.
    node.ensureSessionKey()
    deadline = time.monotonic() + 5
    while (interface.nodesByNum or {}).get(interface.myInfo.my_node_num, {}).get("adminSessionPassKey") is None:
        if time.monotonic() >= deadline or not interface.isConnected.is_set():
            raise CommandError("config_failed")
        time.sleep(0.05)


def write_config(interface, channels, lora):
    from meshtastic.protobuf import channel_pb2
    node = interface.localNode
    ensure_admin_session(interface)
    node.beginSettingsTransaction()
    node.channels = channels + [channel_pb2.Channel(index=index, role=0) for index in range(len(channels), 8)]
    for index in range(8):
        node.writeChannel(index)
    node.localConfig.lora.CopyFrom(lora)
    node.writeConfig("lora")
    node.commitSettingsTransaction()


def validate_target(node, index, expected):
    current = channel_slot(node, index)
    serialized = current.SerializeToString(deterministic=True) if current is not None else None
    if serialized != expected:
        raise CommandError("preview_expired")


def write_channel(interface, target, expected):
    from meshtastic.protobuf import channel_pb2
    node = interface.localNode
    validate_target(node, target.index, expected)
    ensure_admin_session(interface)
    # The read-only session request may have refreshed channels while waiting.
    validate_target(node, target.index, expected)
    current = {channel.index: channel for channel in node.channels or []}
    current[target.index] = target
    node.beginSettingsTransaction()
    node.channels = [current[index] if index in current else channel_pb2.Channel(index=index, role=0)
                     for index in range(max(current) + 1)]
    node.writeChannel(target.index)
    node.commitSettingsTransaction()


def diagnostic_enum(enum, value):
    if type(value) is int:
        return enum_name(enum, value, "")
    return value if isinstance(value, str) and value in enum.keys() else ""


def known_peer(node_num, raw, observed=None):
    from meshtastic.protobuf import config_pb2, mesh_pb2
    node = mapping(raw)
    user = mapping(node.get("user"))
    metrics = mapping(node.get("deviceMetrics"))
    position = mapping(node.get("position"))
    latest = mapping(observed) or mapping(node.get("lastReceived"))
    last_heard = number(integer(latest.get("rxTime", node.get("lastHeard"))), 1, 0xFFFFFFFF)
    hops = number(integer(node.get("hopsAway")), 0, 7)
    start, remaining = integer(latest.get("hopStart")), integer(latest.get("hopLimit"))
    if 0 <= remaining <= start <= 7:
        hops = start - remaining
    latitude = number(position.get("latitude"), -90, 90)
    longitude = number(position.get("longitude"), -180, 180)
    if latitude is None:
        latitude_i = number(position.get("latitudeI"), -900000000, 900000000)
        latitude = latitude_i / 1e7 if latitude_i is not None else None
    if longitude is None:
        longitude_i = number(position.get("longitudeI"), -1800000000, 1800000000)
        longitude = longitude_i / 1e7 if longitude_i is not None else None
    if latitude is None or longitude is None:
        latitude = longitude = None
    return {"id": f"!{node_num:08x}", "name": clean_text(user.get("longName"), 80)
            or clean_text(user.get("shortName"), 16) or f"!{node_num:08x}",
            "hardwareModel": diagnostic_enum(mesh_pb2.HardwareModel, user.get("hwModel")),
            "role": diagnostic_enum(config_pb2.Config.DeviceConfig.Role, user.get("role")),
            "lastHeard": int(last_heard) if last_heard is not None else None,
            "snr": number(latest.get("rxSnr", node.get("snr")), -100, 100),
            "rssi": number(latest.get("rxRssi", node.get("rssi")), -250, 0),
            "hopsAway": int(hops) if hops is not None else None,
            "viaMqtt": latest.get("viaMqtt", node.get("viaMqtt", False)) is True,
            "batteryLevel": number(metrics.get("batteryLevel"), 0, 101),
            "voltage": number(metrics.get("voltage"), 0, 100),
            "latitude": latitude, "longitude": longitude}


class Bridge:
    def __init__(self, history):
        self.history = history
        self.lock = asyncio.Lock()
        self.clients = set()
        self.interface = None
        self.connected = False
        self.error = "radio_unavailable"
        self.sending = False
        self.generation = 0
        self.connected_at = 0
        self.pending = {}
        self.previews = {}
        self.events = asyncio.Queue(maxsize=1024)
        self.notifications_enabled = history.get("notifications", True)
        snapshot = mapping(history.get("snapshot", {}))
        defaults = device_defaults()
        stored = mapping(snapshot.get("device"))
        self.device = {key: stored.get(key, value) for key, value in defaults.items()}
        self.device["connectedSince"] = None
        selected = os.environ.get("MESHTASTIC_TRANSPORT", "usb")
        if selected in ("usb", "ble"):
            previous = self.device["transport"]
            self.device["transport"] = selected
            if previous != selected:
                self.device["endpoint"] = ""
                self.device["port"] = ""
            if selected == "ble":
                endpoint = os.environ.get("MESHTASTIC_BLE_ADDRESS", "")
                if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", endpoint):
                    self.device["endpoint"] = endpoint
            elif not self.device["endpoint"]:
                self.device["endpoint"] = self.device["port"]
        else:
            self.error = "transport_invalid"
        self.channels = snapshot.get("channels", [])
        self.diagnostics = diagnostic_defaults()
        saved_diagnostics = mapping(snapshot.get("diagnostics"))
        self.diagnostics.update({key: saved_diagnostics[key] for key in self.diagnostics if key in saved_diagnostics})
        self.nodes = snapshot.get("nodes", [])[:MAX_NODES]
        self._observations = {}
        self._heard = set()
        self._rx_seen = {}
        self.notifications = Notifications(lambda: self.notifications_enabled)
        self.reconnect = asyncio.Event()
        self.loop = None

    def state(self):
        channels, messages = [], []
        for channel in self.channels:
            channels.append(dict(channel))
            channels[-1]["unread"] = self.history.unread(channel["identity"])
            messages.extend(self.history.messages(channel["identity"]))
        messages.sort(key=lambda message: (message["timestamp"], message["id"]))
        return {"type": "state", "connected": self.connected, "error": self.error,
                "device": self.device, "channels": channels, "messages": messages,
                "diagnostics": self.diagnostics, "nodes": self.nodes,
                "sending": self.sending, "notificationsEnabled": self.notifications_enabled}

    def publish(self):
        data = encode(self.state())
        for client in tuple(self.clients):
            client.offer(data)

    def emit(self, kind, interface, data=None):
        def enqueue():
            try:
                self.events.put_nowait((kind, interface, data))
            except asyncio.QueueFull:
                LOG.error("Radio event queue full; reconnecting without replaying sends")
                self.reconnect.set()
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(enqueue)

    def channel(self, index):
        return next((channel for channel in self.channels if channel["index"] == index), None)

    def metadata(self, interface, endpoint, transport="usb"):
        lora = interface.localNode.localConfig.lora
        radio = f"!{interface.myInfo.my_node_num:08x}"
        self.device = device_defaults()
        self.device.update(id=radio, transport=transport, endpoint=clean_text(endpoint, 256),
                           port=clean_text(endpoint, 256) if transport == "usb" else "")
        self.diagnostics = diagnostic_defaults()
        self._observations.clear()
        self._heard.clear()
        self._rx_seen.clear()
        self.channels = []
        for channel in interface.localNode.channels or []:
            if channel.role in (1, 2):
                self.channels.append(dict(describe_channel(channel, lora), identity=channel_identity(radio, channel, lora)))
        self.refresh_diagnostics(interface)
        self.save_snapshot()
        self.history.put("lora", lora.SerializeToString().hex())

    def save_snapshot(self):
        self.history.put("snapshot", {"device": self.device, "channels": self.channels,
                                     "diagnostics": self.diagnostics, "nodes": self.nodes})

    def refresh_diagnostics(self, interface):
        from meshtastic.protobuf import config_pb2, mesh_pb2
        nodes = interface.nodesByNum or {}
        info = mapping(nodes.get(interface.myInfo.my_node_num))
        if not info:
            info = mapping(interface.getMyNodeInfo())
        user = mapping(info.get("user"))
        metrics = mapping(info.get("deviceMetrics"))
        config = interface.localNode.localConfig
        lora = config.lora
        metadata = getattr(interface, "metadata", None)
        self.device.update(
            name=clean_text(user.get("longName"), 80) or self.device["id"],
            firmware=clean_text(getattr(metadata, "firmware_version", ""), 80),
            hardwareModel=diagnostic_enum(mesh_pb2.HardwareModel, user.get("hwModel"))
                or (enum_name(mesh_pb2.HardwareModel, metadata.hw_model, "") if metadata is not None
                    and hasattr(metadata, "hw_model") else ""),
            role=enum_name(config_pb2.Config.DeviceConfig.Role, config.device.role, "")
                if config.HasField("device") else diagnostic_enum(config_pb2.Config.DeviceConfig.Role, user.get("role")),
            region=lora.region, regionName=enum_name(config_pb2.Config.LoRaConfig.RegionCode, lora.region),
            modemPreset=preset_name(lora.modem_preset), txEnabled=lora.tx_enabled,
            frequencySlot=number(lora.channel_num, 0, 0xFFFFFFFF) if config.HasField("lora") else None,
            # SDK config does not expose firmware's resolved automatic
            # frequency/slot. Do not invent a region/preset frequency table.
            frequencyMHz=None,
            hopLimit=number(lora.hop_limit, 0, 7) if config.HasField("lora") else None,
            txPower=number(lora.tx_power, -128, 127) if config.HasField("lora") else None,
            batteryLevel=number(metrics.get("batteryLevel"), 0, 101),
            voltage=number(metrics.get("voltage"), 0, 100),
            uptimeSeconds=number(metrics.get("uptimeSeconds"), 0, 0xFFFFFFFF),
            channelUtilization=number(metrics.get("channelUtilization"), 0, 100),
            airUtilTx=number(metrics.get("airUtilTx"), 0, 100),
            bluetoothEnabled=config.bluetooth.enabled if config.HasField("bluetooth") else None,
            nodeCount=len(nodes))
        self.nodes = [known_peer(node_num, node, self._observations.get(node_num))
                      for node_num, node in list(nodes.items())
                      if type(node_num) is int and 0 < node_num < BROADCAST
                      and node_num != interface.myInfo.my_node_num]
        self.nodes.sort(key=lambda node: (node["lastHeard"] or 0, node["id"]), reverse=True)
        del self.nodes[MAX_NODES:]
        queue = getattr(interface, "queueStatus", None)
        self.diagnostics.update(updatedAt=int(time.time()),
            queueFree=number(integer(getattr(queue, "free", None)), 0, 0xFFFFFFFF),
            queueMax=number(integer(getattr(queue, "maxlen", None)), 0, 0xFFFFFFFF))

    def observe_packet(self, packet):
        sender = integer(packet.get("from"))
        received = integer(packet.get("rxTime"), 0)
        now = int(time.time())
        if 0 < sender < BROADCAST and 0 < received <= now + 5:
            previous = self._observations.get(sender)
            if previous is None or received >= previous["rxTime"]:
                # Never retain raw payload/admin dictionaries with session keys.
                self._observations.pop(sender, None)
                self._observations[sender] = {key: packet[key] for key in
                    ("rxTime", "rxSnr", "rxRssi", "hopStart", "hopLimit", "viaMqtt") if key in packet}
                self._observations[sender]["viaMqtt"] = bool(packet.get("viaMqtt", False))
                if len(self._observations) > MAX_NODES:
                    del self._observations[next(iter(self._observations))]
        fresh = (self.connected and 0 < sender < BROADCAST
                 and self.device["id"] != f"!{sender:08x}" and not packet.get("viaMqtt")
                 and self.connected_at <= received <= now + 5)
        if fresh:
            signature = (sender, integer(packet.get("id")), received)
            if signature not in self._rx_seen:
                self._rx_seen[signature] = None
                if len(self._rx_seen) > 2048:
                    del self._rx_seen[next(iter(self._rx_seen))]
                self._heard.add(sender)
                self.diagnostics["receivedRadioPackets"] += 1
                self.diagnostics["heardSinceConnect"] = len(self._heard)
                if received >= (self.diagnostics["lastRadioRxAt"] or 0):
                    self.diagnostics.update(lastRadioRxAt=received, lastRadioRxFrom=f"!{sender:08x}",
                        lastRadioRxSnr=number(packet.get("rxSnr"), -100, 100),
                        lastRadioRxRssi=number(packet.get("rxRssi"), -250, 0))
        if self.interface is not None:
            self.refresh_diagnostics(self.interface)

    async def disconnect(self, error="radio_disconnected"):
        was_connected = self.connected
        self.connected = False
        self.device["connectedSince"] = None
        self.error = error
        self.generation += 1
        self.previews.clear()
        for message_id, _deadline in self.pending.values():
            self.history.update(message_id, "unconfirmed", "ack_timeout")
        self.pending.clear()
        if was_connected:
            self.save_snapshot()
        self.publish()

    def expire_acks(self):
        changed = False
        now = time.monotonic()
        for packet_id, (message_id, deadline) in tuple(self.pending.items()):
            if deadline <= now:
                self.history.update(message_id, "unconfirmed", "ack_timeout")
                del self.pending[packet_id]
                changed = True
        if changed:
            self.publish()

    async def connect_loop(self):
        from pubsub import pub
        self.loop = asyncio.get_running_loop()
        def receive(packet, interface):
            self.emit("packet", interface, packet)
        def lost(interface):
            self.emit("lost", interface)
        def node_updated(node, interface):
            self.emit("nodes", interface)
        pub.subscribe(receive, "meshtastic.receive")
        pub.subscribe(lost, "meshtastic.connection.lost")
        pub.subscribe(node_updated, "meshtastic.node.updated")
        try:
            while True:
                interface = None
                self.reconnect.clear()
                async with self.lock:
                    self.previews = {owner: preview for owner, preview in self.previews.items()
                                     if preview.expires_at > time.monotonic()}
                try:
                    transport = transport_selection()
                    opening = asyncio.create_task(asyncio.to_thread(open_radio, transport, self.emit))
                    try:
                        interface, endpoint = await asyncio.shield(opening)
                    except asyncio.CancelledError:
                        # Reap the worker constructor before releasing ownership.
                        with contextlib.suppress(Exception):
                            interface, endpoint = await opening
                        raise
                    self.interface = interface
                    async with self.lock:
                        if not interface.isConnected.is_set():
                            raise CommandError("radio_disconnected")
                        self.connected_at = int(time.time())
                        self.metadata(interface, endpoint, transport)
                        self.device["connectedSince"] = self.connected_at
                        self.connected = True
                        self.error = ""
                        self.generation += 1
                        self.previews.clear()
                        self.publish()
                    while not self.reconnect.is_set():
                        try:
                            await asyncio.wait_for(self.reconnect.wait(), 1)
                        except asyncio.TimeoutError:
                            if not interface.isConnected.is_set() or (transport == "usb" and not Path(endpoint).exists()):
                                break
                            async with self.lock:
                                self.previews = {owner: preview for owner, preview in self.previews.items()
                                                 if preview.expires_at > time.monotonic()}
                                self.expire_acks()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    code = (str(error) if isinstance(error, CommandError) else
                            ble_error(error) if os.environ.get("MESHTASTIC_TRANSPORT") == "ble" else "radio_unavailable")
                    LOG.warning("Radio connection unavailable (%s)", code)
                    async with self.lock:
                        await self.disconnect(code)
                finally:
                    async with self.lock:
                        if self.connected:
                            await self.disconnect()
                        self.interface = None
                    if interface is not None:
                        # Never reconnect with a previous transport still owned.
                        await asyncio.to_thread(interface.close)
                await asyncio.sleep(3)
        finally:
            pub.unsubscribe(receive, "meshtastic.receive")
            pub.unsubscribe(lost, "meshtastic.connection.lost")
            pub.unsubscribe(node_updated, "meshtastic.node.updated")

    async def event_loop(self):
        while True:
            kind, interface, data = await self.events.get()
            async with self.lock:
                if interface is not self.interface:
                    continue
                try:
                    if kind in ("lost", "transport_error"):
                        await self.disconnect(data if kind == "transport_error" else "radio_disconnected")
                        self.reconnect.set()
                    elif kind == "rejected":
                        self.routing(data, False, "send_failed")
                    elif kind in ("nodes", "diagnostics") and self.connected:
                        self.refresh_diagnostics(interface)
                        self.publish()
                    elif kind == "packet" and self.connected:
                        self.receive(data)
                except (sqlite3.Error, ValueError, TypeError, UnicodeError):
                    LOG.error("Cannot process radio event")
                    self.error = "storage_error"
                    self.publish()

    def routing(self, packet_id, acknowledged, error="routing_failed"):
        pending = self.pending.pop(packet_id, None)
        if pending:
            self.history.update(pending[0], "acknowledged" if acknowledged else "failed", "" if acknowledged else error)
            self.publish()

    def receive(self, packet):
        if not isinstance(packet, dict):
            return
        self.observe_packet(packet)
        self._receive_message(packet)
        self.publish()

    def _receive_message(self, packet):
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            return
        port = decoded.get("portnum")
        if port in ("ROUTING_APP", 5):
            routing = decoded.get("routing")
            if isinstance(routing, dict) and integer(decoded.get("requestId")) > 0:
                reason = routing.get("errorReason", "NONE")
                from meshtastic.protobuf import mesh_pb2
                actual = diagnostic_enum(mesh_pb2.Routing.Error, routing.get("errorReason"))
                if actual:
                    self.diagnostics.update(lastRoutingReason=actual, lastRoutingAt=int(time.time()))
                    self.publish()
                self.routing(decoded["requestId"], reason in ("NONE", 0))
            return
        if port not in ("TEXT_MESSAGE_APP", 1) or packet.get("to") != BROADCAST or packet.get("pkiEncrypted"):
            return
        channel = self.channel(integer(packet.get("channel", 0)))
        sender_num, packet_id = integer(packet.get("from")), integer(packet.get("id"))
        if channel is None or not 0 < sender_num < BROADCAST or not 0 < packet_id <= BROADCAST:
            return
        payload = decoded.get("payload")
        if isinstance(payload, bytes):
            text = payload[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")
        else:
            text = decoded.get("text", "")
            if not isinstance(text, str):
                return
            text = text.encode("utf-8", errors="replace")[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")
        text = clean_text(text, MAX_TEXT_BYTES)
        if not text.strip():
            return
        own = self.device["id"] == f"!{sender_num:08x}"
        nodes = self.interface.nodesByNum or {} if self.interface else {}
        sender = clean_text((nodes.get(sender_num, {}).get("user") or {}).get("longName"), 80) or f"!{sender_num:08x}"
        now = int(time.time())
        received = integer(packet.get("rxTime"), 0)
        fresh = self.connected_at <= received <= now + 5
        message_id = self.history.add(radio=self.device["id"], profile=channel["identity"],
            channel=channel["index"], sender_num=sender_num, packet_id=packet_id, sender=sender, text=text,
            timestamp=received if 0 < received <= now + 300 else now,
            outgoing=own, status="unconfirmed" if own else "received", unread=fresh and not own)
        if message_id is not None:
            if fresh and not own:
                self.notifications.enqueue(sender, channel["name"], text)

    def current_lora(self):
        from meshtastic.protobuf import config_pb2
        lora = config_pb2.Config.LoRaConfig()
        if self.connected:
            lora.CopyFrom(self.interface.localNode.localConfig.lora)
        else:
            stored = self.history.get("lora", "")
            if stored:
                lora.ParseFromString(bytes.fromhex(stored))
            else:
                lora.use_preset = True
        return lora

    async def command(self, command, client=None):
        action = command.get("action", "") if isinstance(command, dict) else ""
        request = command.get("requestId", "") if isinstance(command, dict) else ""
        result = {"type": "result", "action": clean_text(action, 40),
                  "requestId": clean_text(request, 128), "ok": False, "error": ""}
        # Refuse concurrent submissions, rather than silently queuing a second send/config.
        if self.sending and action in ("send", "apply_config"):
            result["error"] = "backend_busy"
            return result
        async with self.lock:
            try:
                if not isinstance(command, dict) or not isinstance(action, str) or not isinstance(request, str):
                    raise CommandError("invalid_command")
                if action == "send":
                    await self.send(command)
                elif action == "read":
                    channel = self.channel(integer(command.get("channel")))
                    if channel is None:
                        raise CommandError("channel_unavailable")
                    self.history.read(channel["identity"])
                    self.publish()
                elif action == "notifications":
                    if type(command.get("enabled")) is not bool:
                        raise CommandError("invalid_command")
                    self.notifications_enabled = command["enabled"]
                    self.history.put("notifications", self.notifications_enabled)
                    self.publish()
                elif action in ("preview_config", "preview_berlin"):
                    operation, target_name, target_index, expected = "replace", "", None, None
                    value = command.get("value")
                    if action == "preview_berlin" or (isinstance(value, str) and "://" in value.strip()):
                        config = berlin_config() if action == "preview_berlin" else decode_config(value.strip())
                        channels, lora, warnings = effective_config(config, self.current_lora(), action == "preview_berlin")
                        if not self.connected:
                            warnings.append("offline_preview")
                    else:
                        target_name = command.get("channelName")
                        key = decode_key(value.strip() if isinstance(value, str) else value, target_name)
                        if (not self.connected or self.interface is None
                                or not self.interface.isConnected.is_set()):
                            raise CommandError("radio_unavailable")
                        channels, lora, warnings, operation, target_index, expected = key_config(
                            self.interface, key, target_name)
                    token = secrets.token_urlsafe(24)
                    # One secret-bearing preview per client; replacing/closing releases it.
                    self.previews[client] = ConfigPreview(token, time.monotonic() + PREVIEW_TTL,
                        self.generation, self.device["id"], channels, lora,
                        operation, target_name, target_index, expected)
                    from meshtastic.protobuf import config_pb2
                    result["preview"] = {"token": token, "channels": [describe_channel(c, lora) for c in channels],
                        "regionName": enum_name(config_pb2.Config.LoRaConfig.RegionCode, lora.region),
                        "modemPreset": preset_name(lora.modem_preset), "frequencySlot": lora.channel_num,
                        "txEnabled": lora.tx_enabled, "warnings": warnings, "operation": operation,
                        "targetName": target_name, "targetIndex": target_index}
                elif action == "apply_config":
                    if command.get("confirmed") is not True:
                        raise CommandError("confirmation_required")
                    if self.pending:
                        raise CommandError("backend_busy")
                    preview = self.previews.pop(client, None)
                    if (preview is None or not isinstance(command.get("token"), str)
                            or not secrets.compare_digest(preview.token, command["token"])
                            or preview.expires_at < time.monotonic() or preview.generation != self.generation
                            or preview.radio_id != self.device["id"]):
                        raise CommandError("preview_expired")
                    if (not self.connected or self.interface is None
                            or not self.interface.isConnected.is_set()):
                        raise CommandError("radio_unavailable")
                    if preview.target_index is not None:
                        validate_target(self.interface.localNode, preview.target_index, preview.expected_target)
                    if not preview.lora.region:
                        raise CommandError("region_unset")
                    self.sending = True
                    self.publish()
                    try:
                        if preview.operation == "replace":
                            await asyncio.to_thread(write_config, self.interface, preview.channels, preview.lora)
                        else:
                            target = next(channel for channel in preview.channels if channel.index == preview.target_index)
                            await asyncio.to_thread(write_channel, self.interface, target, preview.expected_target)
                    except Exception as error:
                        code = "preview_expired" if isinstance(error, CommandError) and str(error) == "preview_expired" else "config_failed"
                        raise CommandError(code) from None
                    finally:
                        self.sending = False
                        await self.disconnect()
                        self.reconnect.set()
                else:
                    raise CommandError("invalid_command")
                result["ok"] = True
            except CommandError as error:
                result["error"] = str(error)
            except (sqlite3.Error, OSError, ValueError, TypeError, UnicodeError):
                result["error"] = "storage_error"
                LOG.error("Command could not be completed (%s)", result["action"])
        return result

    async def send(self, command):
        text = command.get("text")
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            raise CommandError("invalid_text")
        try:
            length = len(text.encode("utf-8"))
        except UnicodeError:
            raise CommandError("invalid_text") from None
        if length > MAX_TEXT_BYTES:
            raise CommandError("message_too_long")
        if not self.connected or self.interface is None or not self.interface.isConnected.is_set():
            raise CommandError("radio_unavailable")
        if not self.device["region"]:
            raise CommandError("region_unset")
        if not self.device["txEnabled"]:
            raise CommandError("tx_disabled")
        channel = self.channel(integer(command.get("channel")))
        if channel is None or command.get("channelIdentity") != channel["identity"]:
            raise CommandError("channel_unavailable")
        self.sending = True
        self.publish()
        message_id = None
        try:
            message_id = self.history.add(radio=self.device["id"], profile=channel["identity"],
                channel=channel["index"], sender_num=int(self.device["id"][1:], 16), packet_id=None,
                sender=self.device["name"], text=text, timestamp=int(time.time()), outgoing=True,
                status="failed", error="send_failed")
            packet = await asyncio.to_thread(self.interface.sendText, text, destinationId="^all",
                                            channelIndex=channel["index"], wantAck=True)
            if not packet or not packet.id:
                raise CommandError("send_failed")
            self.history.update(message_id, "queued", packet_id=packet.id)
            self.pending[packet.id] = (message_id, time.monotonic() + ACK_TIMEOUT)
        except Exception as error:
            # The daemon never retries this call. Firmware may already have accepted it.
            if message_id is not None:
                self.history.update(message_id, "failed", "send_failed")
            raise CommandError(str(error) if isinstance(error, CommandError) else "send_failed") from None
        finally:
            self.sending = False
            self.publish()


class Client:
    def __init__(self, writer):
        self.writer = writer
        self.queue = asyncio.Queue(maxsize=4)

    def offer(self, data):
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            # A stalled UI must not block the radio or lose a send result silently.
            self.writer.close()

    async def output(self):
        try:
            while True:
                self.writer.write(await self.queue.get())
                await asyncio.wait_for(self.writer.drain(), timeout=5)
                self.queue.task_done()
        finally:
            self.writer.close()


def encode(value):
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()


async def serve_client(bridge, reader, writer):
    client = Client(writer)
    output = None
    try:
        peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        if struct.unpack("3i", peer)[1] != os.getuid() or len(bridge.clients) >= 16:
            return
        async with bridge.lock:
            bridge.clients.add(client)
            client.offer(encode(bridge.state()))
        output = asyncio.create_task(client.output())
        while not writer.is_closing():
            line = await reader.readline()
            if not line:
                break
            if len(line) > MAX_COMMAND_BYTES:
                break
            try:
                command = json.loads(line)
            except (ValueError, UnicodeError):
                command = None
            result = await bridge.command(command, client)
            client.offer(encode(result))
    except (ConnectionError, ValueError, OSError, asyncio.TimeoutError):
        pass
    finally:
        bridge.clients.discard(client)
        bridge.previews.pop(client, None)
        if output and not writer.is_closing():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(client.queue.join(), 5)
        if output:
            output.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionError, asyncio.TimeoutError):
                await output
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


def socket_path():
    directory = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("A private XDG_RUNTIME_DIR is required")
    return directory / "omarchy-meshtastic.sock"


async def daemon():
    os.umask(0o077)
    path = socket_path()
    lock_fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        raise RuntimeError("Meshtastic daemon is already running") from None
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("Refusing to replace non-socket runtime path")
        path.unlink()
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    history = History(data / "omarchy-meshtastic/history.sqlite3")
    bridge = Bridge(history)
    server = await asyncio.start_unix_server(lambda r, w: serve_client(bridge, r, w), path=str(path), limit=MAX_COMMAND_BYTES)
    os.chmod(path, 0o600)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopping.set)
    tasks = [asyncio.create_task(bridge.connect_loop()), asyncio.create_task(bridge.event_loop()),
             asyncio.create_task(bridge.notifications.run())]
    stop_task = asyncio.create_task(stopping.wait())
    try:
        done, _ = await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
        if stop_task not in done:
            raise RuntimeError("Backend worker stopped")
    finally:
        # Python 3.13 waits for every client transport, including handlers
        # still waiting for the bridge lock. Abort them before waiting so a
        # stalled UI/buffer cannot keep the radio owner alive on shutdown.
        server.close()
        server.abort_clients()
        await server.wait_closed()
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        history.db.close()
        path.unlink(missing_ok=True)
        os.close(lock_fd)


async def stdio_client():
    reader, writer = await asyncio.open_unix_connection(str(socket_path()), limit=MAX_STATE_BYTES)
    stdin = asyncio.StreamReader(limit=MAX_COMMAND_BYTES)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin.buffer)

    async def upload():
        while True:
            line = await stdin.readline()
            if not line:
                return
            if len(line) > MAX_COMMAND_BYTES:
                raise ValueError("Command too large")
            writer.write(line if line.endswith(b"\n") else line + b"\n")
            await asyncio.wait_for(writer.drain(), 5)

    async def download():
        while True:
            line = await reader.readline()
            if not line:
                return
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if tasks[0] in done and tasks[1] not in done:
            writer.write_eof()
            await tasks[1]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.close()
        writer.close()
        await writer.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("daemon", "client"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(name)s: %(message)s")
    # SDK prints and diagnostics must never enter the client's JSONL stream.
    if args.mode == "daemon":
        sys.stdout = sys.stderr
        logging.getLogger("meshtastic").setLevel(logging.CRITICAL)
    try:
        asyncio.run(daemon() if args.mode == "daemon" else stdio_client())
    except (OSError, RuntimeError, ValueError) as error:
        LOG.error("%s", clean_text(str(error), 160))
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
