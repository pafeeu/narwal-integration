"""Coordinator for ThingModel robots (Narwal Freo S).

Reads are ``getReported`` plus property pushes. A 3 s heartbeat runs only
while a task is active, matching the app's device-page keepalive without
keeping a docked robot awake. Command timeouts never fail setup.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

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
    DEFAULT_CLEAN_MODE,
    DEFAULT_PASSES,
    DEFAULT_SUCTION,
    DEFAULT_WATER,
    DOMAIN,
    OPT_CLEAN_MODE,
    OPT_PASSES,
    OPT_SUCTION,
    OPT_WATER,
    PROTOCOL_THING,
)
from .coordinator import MAX_CONSECUTIVE_FAILURES, POLL_INTERVAL_ACTIVE, POLL_INTERVAL_IDLE
from .narwal_client import NarwalCloud, NarwalCloudError, NarwalCommandError, NarwalConnectionError
from .narwal_client.const import MQTT_BROKER
from .narwal_client.thing_client import ThingClient
from .narwal_client.thing_protocol import (
    ThingState,
    SUCTION_LEVELS,
    SWEEP_BY_TYPE,
    SWEEP_TYPES,
    WATER_LEVELS,
    delete_schedule_id,
    encode_schedules,
    model_name,
    once_timestamp,
    parse_timing_value,
    schedule_zone,
    set_schedule_enabled,
    upsert_one_time,
)

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = timedelta(seconds=3)
CONF_THING_CACHE = "thing_cache"
SERVICE_SET_SCHEDULES = "set_schedules"
SERVICE_SCHEDULE_ONCE = "schedule_clean_once"
SERVICE_DELETE_SCHEDULE = "delete_schedule"

SET_SCHEDULES_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("timing"): cv.string,
    vol.Optional("schedule_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional("enabled"): bool,
})

SCHEDULE_ONCE_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Required("date"): cv.string,
    vol.Required("time"): cv.string,
    vol.Optional("rooms"): [vol.Any(str, int)],
    vol.Optional("areas"): [cv.string],
    vol.Optional("mode"): cv.string,
    vol.Optional("suction"): cv.string,
    vol.Optional("water"): cv.string,
    vol.Optional("passes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=2)),
})

DELETE_SCHEDULE_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Required("schedule_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
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
        # Preferences for the next start_sweep. Loaded from the options flow.
        self.suction_level: int = SUCTION_LEVELS[DEFAULT_SUCTION]
        self.water_level: int = WATER_LEVELS[DEFAULT_WATER]
        self.sweep_type: int = SWEEP_TYPES[DEFAULT_CLEAN_MODE]
        self.passes: int = DEFAULT_PASSES
        self._setup_client(entry)
        self.load_options()

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

    def load_options(self) -> None:
        """Apply the options-flow defaults. Missing keys use the Freo S defaults."""
        opts = self.config_entry.options
        mode = str(opts.get(OPT_CLEAN_MODE, DEFAULT_CLEAN_MODE))
        self.sweep_type = SWEEP_TYPES.get(mode, SWEEP_TYPES[DEFAULT_CLEAN_MODE])
        try:
            passes = int(opts.get(OPT_PASSES, DEFAULT_PASSES))
        except (TypeError, ValueError):
            passes = DEFAULT_PASSES
        self.passes = min(2, max(1, passes))
        suction = str(opts.get(OPT_SUCTION, DEFAULT_SUCTION))
        self.suction_level = SUCTION_LEVELS.get(suction, SUCTION_LEVELS[DEFAULT_SUCTION])
        water = str(opts.get(OPT_WATER, DEFAULT_WATER))
        self.water_level = WATER_LEVELS.get(water, WATER_LEVELS[DEFAULT_WATER])

    def set_pref(self, **changes: Any) -> None:
        """Store one default and use it for the next clean."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            options={**self.config_entry.options, **changes},
        )
        self.load_options()

    def effective_suction(self) -> int:
        return self.suction_level

    def effective_water(self) -> int:
        return self.water_level

    def clean_mode_name(self) -> str:
        return SWEEP_BY_TYPE.get(self.sweep_type, DEFAULT_CLEAN_MODE)

    async def async_setup(self) -> None:
        """Connect and load state. A sleeping robot must not fail setup."""
        self._restore_cache()
        if self._cloud:
            await self._reauth()
        # Hold pushes until the first read, including the map, has landed.
        # Otherwise a status reply can publish entities with no rooms yet.
        self._defer_updates = True
        self.client.on_state_update = self._on_state_update
        self.config_entry.async_on_unload(
            self.config_entry.add_update_listener(self._async_options_updated)
        )
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

    async def _async_options_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Apply options without reloading the MQTT connection."""
        self.load_options()
        self.async_set_updated_data(self.client.state)

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

    def schedule_tz(self):
        """HA local zone for one-time schedule write/display."""
        return schedule_zone(self.hass.config.time_zone)

    async def async_schedule_once(
        self,
        *,
        date: str,
        clock: str,
        rooms: list[int],
        suction: int,
        water: int,
        sweep_type: int,
        passes: int,
    ) -> None:
        """Read the list, enable one one-shot row, write the whole list back.

        ``date`` / ``clock`` are Home Assistant local wall time. The epoch on
        the wire is UTC so the Narwal app shows the same local clock.
        """
        if not rooms:
            raise NarwalCommandError("A one-time clean needs at least one room")
        map_id = self.client.state.map_id
        if not map_id:
            raise NarwalCommandError("The robot has not reported its map yet")
        current = await self.client.read_timing()
        try:
            updated = upsert_one_time(
                parse_timing_value(current),
                when=once_timestamp(date, clock, tz=self.schedule_tz()),
                rooms=rooms,
                map_id=map_id,
                suction=suction,
                water=water,
                sweep_type=sweep_type,
                passes=passes,
            )
        except ValueError as err:
            raise NarwalCommandError(str(err)) from err
        await self.client.write_timing(updated)

    async def async_delete_schedule(self, schedule_id: int) -> None:
        """Read the list, drop one entry, write the rest back."""
        current = await self.client.read_timing()
        try:
            updated = delete_schedule_id(parse_timing_value(current), schedule_id)
        except ValueError as err:
            raise NarwalCommandError(str(err)) from err
        await self.client.write_timing(updated)

    async def async_shutdown(self) -> None:
        if self._heartbeat_unsub is not None:
            self._heartbeat_unsub()
            self._heartbeat_unsub = None
        await super().async_shutdown()
        await self.client.disconnect()


def _coordinator_for_entity(hass: HomeAssistant, entity_id: str) -> ThingCoordinator:
    entity = er.async_get(hass).async_get(entity_id)
    entry = hass.config_entries.async_get_entry(entity.config_entry_id) if entity and entity.config_entry_id else None
    coordinator = entry.runtime_data if entry is not None else None
    if not isinstance(coordinator, ThingCoordinator):
        raise HomeAssistantError("Schedules can be written on a Narwal Freo S only.")
    return coordinator


def _rooms_from_call(coordinator: ThingCoordinator, data: dict[str, Any], entity_id: str) -> list[int]:
    """Room ids from names, ids, and Home Assistant areas mapped on the vacuum."""
    hass = coordinator.hass
    found: list[int] = []
    entity = er.async_get(hass).async_get(entity_id)
    mapping = {}
    if entity is not None:
        mapping = (entity.options.get("vacuum") or {}).get("area_mapping") or {}
    for area_id in data.get("areas") or []:
        segments = mapping.get(area_id)
        if not segments:
            raise NarwalCommandError(
                f"Area {area_id} is not mapped on this vacuum. "
                "Map vacuum segments to areas in the vacuum entity settings first."
            )
        found.extend(int(segment) for segment in segments)
    for item in data.get("rooms") or []:
        text = str(item).strip()
        if text.isdigit():
            found.append(int(text))
            continue
        matches = [
            room.room_id for room in coordinator.client.state.rooms
            if room.display_name.casefold() == text.casefold()
        ]
        if len(matches) != 1:
            known = [room.display_name for room in coordinator.client.state.rooms]
            raise NarwalCommandError(f"Room {text!r} did not match one map room; known names: {known}")
        found.append(matches[0])
    # Keep order, drop duplicates.
    unique: list[int] = []
    for room_id in found:
        if room_id not in unique:
            unique.append(room_id)
    return unique


async def async_handle_set_schedules(call: ServiceCall) -> None:
    """Replace the Freo S schedule list, or enable/disable one entry.

    Either ``timing`` (the full ``;``-separated list) or ``schedule_id`` plus
    ``enabled``. Both paths write the whole list back. The read happens first,
    so a failed read never writes a partial list.
    """
    coordinator = _coordinator_for_entity(call.hass, call.data["entity_id"])
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


async def async_handle_schedule_once(call: ServiceCall) -> None:
    """Add or reuse one enabled one-shot schedule entry."""
    coordinator = _coordinator_for_entity(call.hass, call.data["entity_id"])
    data = call.data
    try:
        rooms = _rooms_from_call(coordinator, data, data["entity_id"])
        mode = data.get("mode", coordinator.clean_mode_name())
        if mode not in SWEEP_TYPES:
            raise NarwalCommandError(f"Unknown mode {mode!r}; use one of {list(SWEEP_TYPES)}")
        suction_name = data.get("suction", {v: k for k, v in SUCTION_LEVELS.items()}.get(coordinator.suction_level, DEFAULT_SUCTION))
        if suction_name not in SUCTION_LEVELS:
            raise NarwalCommandError(f"Unknown suction {suction_name!r}; use one of {list(SUCTION_LEVELS)}")
        water_name = data.get("water", {v: k for k, v in WATER_LEVELS.items()}.get(coordinator.water_level, DEFAULT_WATER))
        if water_name not in WATER_LEVELS:
            raise NarwalCommandError(f"Unknown water {water_name!r}; use one of {list(WATER_LEVELS)}")
        passes = int(data.get("passes", coordinator.passes))
        await coordinator.async_schedule_once(
            date=data["date"],
            clock=data["time"],
            rooms=rooms,
            suction=SUCTION_LEVELS[suction_name],
            water=WATER_LEVELS[water_name],
            sweep_type=SWEEP_TYPES[mode],
            passes=passes,
        )
    except NarwalCommandError as err:
        raise HomeAssistantError(str(err)) from err
    coordinator.async_set_updated_data(coordinator.client.state)


async def async_handle_delete_schedule(call: ServiceCall) -> None:
    """Remove one schedule entry by rewriting the rest of the list."""
    coordinator = _coordinator_for_entity(call.hass, call.data["entity_id"])
    try:
        await coordinator.async_delete_schedule(int(call.data["schedule_id"]))
    except NarwalCommandError as err:
        raise HomeAssistantError(str(err)) from err
    coordinator.async_set_updated_data(coordinator.client.state)
