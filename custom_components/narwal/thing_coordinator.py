"""Coordinator for ThingModel robots (Narwal Freo S).

Reads are ``getReported`` plus property pushes. A 3 s heartbeat runs only
while a task is active, matching the app's device-page keepalive without
keeping a docked robot awake. Command timeouts never fail setup.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_NAME,
    CONF_EMAIL,
    CONF_FIRMWARE,
    CONF_MODEL,
    CONF_MQTT_PASSWORD,
    CONF_PASSWORD,
    CONF_PRODUCT_GROUP,
    CONF_PRODUCT_KEY,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_USER_UUID,
    DOMAIN,
    PROTOCOL_THING,
)
from .coordinator import MAX_CONSECUTIVE_FAILURES, POLL_INTERVAL_ACTIVE, POLL_INTERVAL_IDLE
from .narwal_client import NarwalCloud, NarwalCloudError, NarwalCommandError, NarwalConnectionError
from .narwal_client.const import MQTT_BROKER
from .narwal_client.thing_client import ThingClient
from .narwal_client.thing_protocol import (
    ThingState,
    encode_schedules,
    model_name,
    parse_timing_value,
    set_schedule_enabled,
)

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = timedelta(seconds=3)
CONF_THING_CACHE = "thing_cache"
SERVICE_SET_SCHEDULES = "set_schedules"

SET_SCHEDULES_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("timing"): cv.string,
    vol.Optional("schedule_id"): int,
    vol.Optional("enabled"): bool,
})

# Map last, so the state published at the end of setup already has rooms.
_READS = (
    {"robot_status": []},
    {"clean": []},
    {"map": []},
)


class ThingCoordinator(DataUpdateCoordinator[ThingState]):
    """Polls and receives pushes for one Freo S."""

    config_entry: ConfigEntry
    protocol = PROTOCOL_THING

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_thing", update_interval=POLL_INTERVAL_ACTIVE)
        self.config_entry = entry
        self._cloud: NarwalCloud | None = None
        self._consecutive_failures = 0
        self._heartbeat_unsub = None
        self._heartbeat_running = False
        self._defer_updates = False
        # Preferences applied on the next start_sweep. None follows the robot.
        self.suction_level: int | None = None
        self.water_level: int | None = None
        self.sweep_type: int = 2
        self.passes: int = 1
        self._setup_client(entry)

    def _setup_client(self, entry: ConfigEntry) -> None:
        region = entry.data.get(CONF_REGION, "us")
        if CONF_ACCESS_TOKEN in entry.data:
            self._cloud = NarwalCloud(region=region)
            self._cloud.session.access_token = entry.data[CONF_ACCESS_TOKEN]
            self._cloud.session.refresh_token = entry.data.get(CONF_REFRESH_TOKEN, "")
            self._cloud.session.user_uuid = entry.data[CONF_USER_UUID]
            self._cloud.session.region = region
            self._cloud._update_token_expiry()
            password = entry.data[CONF_ACCESS_TOKEN]
            broker = self._cloud.mqtt_broker
        else:
            password = entry.data[CONF_MQTT_PASSWORD]
            broker = MQTT_BROKER
        self.client = ThingClient(
            product_key=entry.data[CONF_PRODUCT_KEY],
            device_name=entry.data[CONF_DEVICE_NAME],
            user_uuid=entry.data[CONF_USER_UUID],
            mqtt_password=password,
            broker=broker,
        )

    @property
    def model(self) -> str:
        data = self.config_entry.data
        return model_name(
            data.get(CONF_PRODUCT_KEY, ""),
            data.get(CONF_PRODUCT_GROUP, ""),
            data.get(CONF_MODEL, ""),
        ) or "Narwal Freo S"

    @property
    def firmware(self) -> str | None:
        return self.config_entry.data.get(CONF_FIRMWARE) or None

    def effective_suction(self) -> int:
        if self.suction_level is not None:
            return self.suction_level
        reported = self.client.state.suction
        return reported if reported is not None else 2

    def effective_water(self) -> int:
        if self.water_level is not None:
            return self.water_level
        reported = self.client.state.water
        return reported if reported is not None else 2

    async def async_setup(self) -> None:
        """Connect and load state. A sleeping robot must not fail setup."""
        self._restore_cache()
        if self._cloud:
            await self._reauth()
        # Hold pushes until the first read, including the map, has landed.
        # Otherwise a status reply can publish entities with no rooms yet.
        self._defer_updates = True
        self.client.on_state_update = self._on_state_update
        await self.client.connect()
        await self._refresh(setup=True)
        self._defer_updates = False
        if not self.client.state.rooms:
            _LOGGER.warning("Freo S map did not include rooms; room cleaning waits for the next map read")
        self.async_set_updated_data(self.client.state)
        self._heartbeat_unsub = async_track_time_interval(
            self.hass, self._heartbeat, HEARTBEAT_INTERVAL
        )

    def _restore_cache(self) -> None:
        cache = self.config_entry.data.get(CONF_THING_CACHE) or {}
        rooms = cache.get("rooms") or []
        if rooms:
            from .narwal_client.thing_protocol import ThingRoom

            self.client.state.rooms = [
                ThingRoom(room_id=int(item["room_id"]), name=str(item.get("name") or item["room_id"]))
                for item in rooms
                if "room_id" in item
            ]
        map_id = cache.get("map_id")
        if isinstance(map_id, int):
            self.client.state.map_id = map_id

    def _persist_cache(self) -> None:
        state = self.client.state
        if not state.rooms and not state.map_id:
            return
        cache = {
            "rooms": [{"room_id": room.room_id, "name": room.name} for room in state.rooms],
            "map_id": state.map_id,
        }
        if self.config_entry.data.get(CONF_THING_CACHE) == cache:
            return
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, CONF_THING_CACHE: cache}
        )

    async def _refresh(self, *, setup: bool = False) -> bool:
        """Read status, clean (including schedules) and rooms. True if any reply arrived."""
        ok = False
        for properties in _READS:
            try:
                await self.client.get_reported(properties)
                ok = True
            except (NarwalCommandError, NarwalConnectionError) as err:
                level = logging.WARNING if setup else logging.DEBUG
                _LOGGER.log(level, "ThingModel read %s failed: %s", ",".join(properties), err)
        if ok:
            self._consecutive_failures = 0
            self._persist_cache()
        return ok

    async def _heartbeat(self, _now=None) -> None:
        if self._heartbeat_running or not self.client.connected or not self.client.state.task_active:
            return
        self._heartbeat_running = True
        try:
            await self.client.heartbeat()
        except (NarwalCommandError, NarwalConnectionError):
            _LOGGER.debug("ThingModel heartbeat failed", exc_info=True)
        finally:
            self._heartbeat_running = False

    def _on_state_update(self, state: ThingState) -> None:
        if self._defer_updates:
            return
        self._consecutive_failures = 0
        self._adjust_poll_interval()
        self.async_set_updated_data(state)

    def _adjust_poll_interval(self) -> None:
        new_interval = POLL_INTERVAL_ACTIVE if self.client.state.task_active else POLL_INTERVAL_IDLE
        if self.update_interval != new_interval:
            self.update_interval = new_interval

    async def _async_update_data(self) -> ThingState:
        if self._cloud and (
            self._cloud.session.is_token_expired
            or self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES
        ):
            await self._reconnect()
        if not self.client.connected:
            self._consecutive_failures += 1
            return self.client.state
        if not await self._refresh():
            self._consecutive_failures += 1
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                await self._reconnect()
        self._adjust_poll_interval()
        return self.client.state

    async def _reauth(self) -> None:
        if not self._cloud:
            return
        try:
            session = await self.hass.async_add_executor_job(self._cloud.refresh_token)
            self._apply_token(session.access_token, session.refresh_token)
            return
        except NarwalCloudError as err:
            _LOGGER.info("Token refresh failed (%s), attempting full re-login", err)
        email = self.config_entry.data.get(CONF_EMAIL)
        password = self.config_entry.data.get(CONF_PASSWORD)
        if not email or not password:
            _LOGGER.error("Cannot re-login: no stored credentials")
            return
        try:
            session = await self.hass.async_add_executor_job(self._cloud.login, email, password)
            self._apply_token(session.access_token, session.refresh_token)
        except NarwalCloudError as err:
            _LOGGER.error("Re-login failed: %s", err)

    def _apply_token(self, access_token: str, refresh_token: str) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_ACCESS_TOKEN: access_token,
                CONF_REFRESH_TOKEN: refresh_token,
            },
        )
        self.client.mqtt_password = access_token

    async def _reconnect(self) -> None:
        _LOGGER.warning("Refreshing ThingModel connection (failures=%d)", self._consecutive_failures)
        try:
            await self._reauth()
            await self.client.disconnect()
            await self.client.connect()
            self._consecutive_failures = 0
        except Exception:
            _LOGGER.error("ThingModel reconnect failed", exc_info=True)
            self.client.state.device_reachable = False
            self.async_set_updated_data(self.client.state)

    async def async_replace_schedules(self, timing: str) -> None:
        """Write a whole schedule list. The string must round-trip the known format."""
        inner = timing.strip()
        try:
            entries = parse_timing_value(inner)
        except ValueError as err:
            raise NarwalCommandError(str(err)) from err
        if not entries:
            raise NarwalCommandError("Refusing to write an empty schedule list")
        if encode_schedules(entries) != inner:
            raise NarwalCommandError("Schedule list does not match the Freo S timing format")
        await self.client.write_timing(inner)

    async def async_set_schedule_enabled(self, schedule_id: int, enabled: bool) -> None:
        """Read the list, flip one entry, write the whole list back."""
        current = await self.client.read_timing()
        try:
            entries = parse_timing_value(current)
            updated = set_schedule_enabled(entries, schedule_id, enabled)
        except ValueError as err:
            raise NarwalCommandError(str(err)) from err
        if updated == current:
            return
        await self.client.write_timing(updated)

    async def async_shutdown(self) -> None:
        if self._heartbeat_unsub is not None:
            self._heartbeat_unsub()
            self._heartbeat_unsub = None
        await super().async_shutdown()
        await self.client.disconnect()


async def async_handle_set_schedules(call: ServiceCall) -> None:
    """Replace the Freo S schedule list, or enable/disable one entry.

    Either ``timing`` (the full ``;``-separated list) or ``schedule_id`` plus
    ``enabled``. Both paths write the whole list back. The read happens first,
    so a failed read never writes a partial list.
    """
    hass = call.hass
    entity = er.async_get(hass).async_get(call.data["entity_id"])
    entry = hass.config_entries.async_get_entry(entity.config_entry_id) if entity and entity.config_entry_id else None
    coordinator = entry.runtime_data if entry is not None else None
    if not isinstance(coordinator, ThingCoordinator):
        raise HomeAssistantError("Schedules can be written on a Narwal Freo S only.")
    timing = call.data.get("timing")
    schedule_id = call.data.get("schedule_id")
    try:
        if timing:
            if schedule_id is not None:
                raise NarwalCommandError("Pass either timing or schedule_id, not both")
            await coordinator.async_replace_schedules(timing)
        elif schedule_id is not None:
            if "enabled" not in call.data:
                raise NarwalCommandError("schedule_id needs enabled true or false")
            await coordinator.async_set_schedule_enabled(int(schedule_id), bool(call.data["enabled"]))
        else:
            raise NarwalCommandError("Pass timing, or schedule_id and enabled")
    except NarwalCommandError as err:
        raise HomeAssistantError(str(err)) from err
    coordinator.async_set_updated_data(coordinator.client.state)
