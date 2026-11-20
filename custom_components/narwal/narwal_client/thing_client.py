"""MQTT client for Narwal ThingModel robots (Freo S / BX3).

Same broker, MQTT 5 and account JWT as the protobuf client. Payloads are
plain JSON and replies are matched on ``messageId``. Publishes use QoS 0:
this broker does not PUBACK QoS 1, and paho then stops sending.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import threading
import uuid
from typing import Any

import paho.mqtt.client as mqtt

from .client import NarwalCommandError, NarwalConnectionError
from .const import MQTT_BROKER, MQTT_PORT
from .thing_protocol import (
    ThingState,
    apply_thing_message,
    build_get_reported,
    build_invoke,
    build_set_property,
    build_start_input,
    decode_map_blob,
    result_code,
    thing_suffix,
    timing_inner,
    wrap_timing,
)

_LOGGER = logging.getLogger(__name__)

PUBLISH_QOS = 0
READ_TIMEOUT = 10.0
ACTION_TIMEOUT = 15.0

# Paths under the single ``thing/`` segment. ``ThingClient.topic`` adds that segment.
_SUBSCRIBE = (
    "property/up/reply",
    "property/down",
    "action/request/reply",
    "action/invoke/reply",
    "event/down",
)


class ThingClient:
    """One ThingModel robot on the Narwal cloud broker."""

    def __init__(
        self,
        *,
        product_key: str,
        device_name: str,
        user_uuid: str,
        mqtt_password: str,
        broker: str = MQTT_BROKER,
        port: int = MQTT_PORT,
    ) -> None:
        self.product_key = product_key
        self.device_name = device_name
        self.user_uuid = user_uuid
        self.mqtt_password = mqtt_password
        self.broker = broker
        self.port = port
        self.state = ThingState()
        self.on_state_update = None  # optional callback(state), from the MQTT thread via the loop
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: mqtt.Client | None = None
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._sub_remaining = 0

    @property
    def base(self) -> str:
        """Device root, ``/<productKey>/<deviceId>``. Does not include ``thing/``."""
        return f"/{self.product_key}/{self.device_name}"

    def topic(self, suffix: str) -> str:
        """Full MQTT topic. ``suffix`` is relative to the one ``thing/`` segment.

        ``property/up`` and ``thing/property/up`` both become
        ``/<productKey>/<deviceId>/thing/property/up``. A suffix that still
        contains ``thing/`` after that is rejected, so the segment cannot be doubled.
        """
        relative = suffix.strip("/")
        if relative == "thing" or relative.startswith("thing/"):
            relative = relative[len("thing"):].lstrip("/")
        if not relative or relative.startswith("thing/") or "/thing/" in f"/{relative}/":
            raise ValueError(f"ThingModel path {suffix!r} must sit under a single thing/ segment")
        return f"{self.base}/thing/{relative}"

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def connect(self) -> None:
        """Connect and subscribe to the explicit thing topics."""
        self._loop = asyncio.get_running_loop()
        self._connected.clear()
        await self._loop.run_in_executor(None, self._connect_blocking)

    def _connect_blocking(self) -> None:
        client_id = f"app_{self.user_uuid}_{uuid.uuid1()}"
        _LOGGER.info("Connecting ThingModel client to %s:%s", self.broker, self.port)
        client = mqtt.Client(
            client_id=client_id,
            protocol=mqtt.MQTTv5,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        client.username_pw_set(self.user_uuid, self.mqtt_password)
        client.tls_set_context(ssl.create_default_context())
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.on_subscribe = self._on_subscribe
        self._client = client
        self._sub_remaining = len(_SUBSCRIBE)
        client.connect(self.broker, self.port, keepalive=30)
        client.loop_start()
        if not self._connected.wait(timeout=15.0):
            client.loop_stop()
            self._client = None
            raise NarwalConnectionError("ThingModel MQTT connection timed out")
        self.state.device_reachable = True

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties=None):
        if not (str(reason_code) == "Success" or reason_code == 0):
            _LOGGER.error("ThingModel MQTT rejected: %s", reason_code)
            return
        for suffix in _SUBSCRIBE:
            client.subscribe(self.topic(suffix), qos=PUBLISH_QOS)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        codes = reason_codes if isinstance(reason_codes, list) else [reason_codes]
        failed = [code for code in codes if getattr(code, "is_failure", False)]
        if failed:
            _LOGGER.warning("ThingModel subscription refused: %s", failed)
        with self._lock:
            self._sub_remaining -= 1
            if self._sub_remaining <= 0:
                self._connected.set()

    def _on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None):
        _LOGGER.warning("ThingModel MQTT disconnected: %s", reason_code)
        self._connected.clear()
        self.state.device_reachable = False
        self._notify()

    def _notify(self) -> None:
        callback = self.on_state_update
        loop = self._loop
        if callback is None or loop is None:
            return
        if loop.is_running():
            try:
                loop.call_soon_threadsafe(callback, self.state)
            except RuntimeError:
                return
        else:
            callback(self.state)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            _LOGGER.debug("Ignoring non-JSON ThingModel payload")
            return
        if not isinstance(data, dict):
            return
        suffix = thing_suffix(msg.topic)
        apply_thing_message(self.state, suffix, data)
        self.state.device_reachable = True
        self._settle(suffix, data)
        self._notify()

    def _settle(self, suffix: str, data: dict[str, Any]) -> None:
        message_id = data.get("messageId")
        if not isinstance(message_id, str):
            return
        with self._lock:
            slot = self._pending.get(message_id)
            if slot is None:
                return
            kind = slot["kind"]
            if kind == "property" and suffix == "property/up/reply":
                slot["reply"] = data
                slot["event"].set()
            elif kind == "action" and suffix == "action/invoke/reply":
                slot["reply"] = data
                slot["event"].set()
            elif kind == "action" and suffix == "action/request/reply":
                slot["cloud_ack"] = data
                if result_code(data) not in (0, None):
                    slot["reply"] = data
                    slot["event"].set()

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        self._connected.clear()

        def _stop() -> None:
            if client is None:
                return
            try:
                client.disconnect()
            except Exception:
                _LOGGER.debug("ThingModel disconnect failed", exc_info=True)
            client.loop_stop()

        loop = self._loop or asyncio.get_running_loop()
        await loop.run_in_executor(None, _stop)

    async def get_reported(self, properties: dict[str, list[str]], *, timeout: float = READ_TIMEOUT) -> dict[str, Any]:
        message_id = str(uuid.uuid1())
        body = build_get_reported(message_id, properties)
        return await self._request("property/up", body, kind="property", timeout=timeout)

    async def invoke(
        self,
        module: str,
        action: str,
        data: dict[str, Any] | None = None,
        *,
        timeout: float = ACTION_TIMEOUT,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid1())
        body = build_invoke(message_id, module, action, data)
        return await self._request("action/request", body, kind="action", timeout=timeout)

    async def set_properties(self, properties: dict[str, Any], *, timeout: float = ACTION_TIMEOUT) -> dict[str, Any]:
        message_id = str(uuid.uuid1())
        body = build_set_property(message_id, properties)
        return await self._request("property/up", body, kind="property", timeout=timeout)

    async def _request(self, suffix: str, body: dict[str, Any], *, kind: str, timeout: float) -> dict[str, Any]:
        if not self._client or not self.connected:
            raise NarwalConnectionError("Not connected")
        loop = self._loop or asyncio.get_running_loop()
        reply = await loop.run_in_executor(None, self._request_blocking, suffix, body, kind, timeout)
        code = result_code(reply)
        if code not in (0, None):
            action = body.get("actionIdentifier") or body.get("method")
            raise NarwalCommandError(f"{action} rejected (code {code})", result=code)
        return reply

    def _request_blocking(self, suffix: str, body: dict[str, Any], kind: str, timeout: float) -> dict[str, Any]:
        message_id = body["messageId"]
        event = threading.Event()
        with self._lock:
            self._pending[message_id] = {"event": event, "kind": kind, "reply": None}
        try:
            assert self._client is not None
            payload = json.dumps(body, separators=(",", ":")).encode()
            self._client.publish(self.topic(suffix), payload, qos=PUBLISH_QOS)
            if not event.wait(timeout):
                raise NarwalCommandError(f"{suffix} timed out", timeout=True)
            with self._lock:
                reply = self._pending[message_id]["reply"]
            if not isinstance(reply, dict):
                raise NarwalCommandError(f"{suffix} returned no reply", timeout=True)
            return reply
        finally:
            with self._lock:
                self._pending.pop(message_id, None)

    async def heartbeat(self) -> dict[str, Any]:
        """App keepalive. Sent while a task is running so pushes keep flowing."""
        return await self.invoke("custom", "heartbeat", {}, timeout=5.0)

    async def read_timing(self) -> str:
        await self.get_reported({"clean": ["timing"]})
        return timing_inner(self.state.raw_timing)

    async def write_timing(self, inner: str) -> None:
        """Replace the whole schedule list. ``inner`` is the ``;``-separated string."""
        wrapped = wrap_timing(inner)
        await self.set_properties({"clean": {"timing": {"value": wrapped}}})
        self.state.merge_modules({"clean": {"timing": {"value": wrapped}}})

    async def start_sweep(
        self,
        room_ids: list[int],
        *,
        map_id: int,
        suction: int,
        water: int,
        sweep_type: int,
        passes: int,
    ) -> dict[str, Any]:
        return await self.invoke(
            "clean",
            "start_sweep",
            build_start_input(
                room_ids=room_ids,
                map_id=map_id,
                suction=suction,
                water=water,
                sweep_type=sweep_type,
                passes=passes,
            ),
        )

    async def pause(self) -> dict[str, Any]:
        return await self.invoke("clean", "pause_sweeping", {})

    async def resume(self) -> dict[str, Any]:
        """``continue_sweep`` uses the same empty invoke envelope as pause.

        The action name is from the app binary; this capture did not include a resume.
        """
        return await self.invoke("clean", "continue_sweep", {})

    async def stop(self) -> dict[str, Any]:
        """``stop_sweeping`` uses the same empty invoke envelope as pause.

        The action name is from the app binary; this capture did not include a stop.
        """
        return await self.invoke("clean", "stop_sweeping", {})

    async def recall(self) -> dict[str, Any]:
        return await self.invoke("clean", "recall_station", {})

    async def get_current_map(self) -> bytes:
        """Return the gzip-decoded ``current_map`` blob from ``get_current_map``."""
        reply = await self.invoke("map", "get_current_map", {})
        output = reply.get("output") or {}
        blob = output.get("current_map")
        if not isinstance(blob, str) or not blob:
            raise NarwalCommandError("Map reply had no current_map")
        try:
            return decode_map_blob(blob)
        except ValueError as err:
            raise NarwalCommandError(str(err)) from err
