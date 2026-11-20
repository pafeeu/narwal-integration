"""Home Assistant entities for the Narwal Freo S (ThingModel / BX3).

These are separate from the protobuf entities. Platform setup calls in here
only when the config entry resolved to the ThingModel protocol.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.const import PERCENTAGE, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .narwal_client import NarwalCommandError, NarwalConnectionError
from .narwal_client.models import (
    ACTIVITY_CLEANING,
    ACTIVITY_DOCKED,
    ACTIVITY_ERROR,
    ACTIVITY_IDLE,
    ACTIVITY_PAUSED,
    ACTIVITY_RETURNING,
)
from .narwal_client.thing_protocol import (
    SUCTION_BY_LEVEL,
    SUCTION_LEVELS,
    SWEEP_BY_TYPE,
    SWEEP_TYPES,
    WATER_BY_LEVEL,
    WATER_LEVELS,
    ThingState,
)
from .thing_coordinator import ThingCoordinator

try:  # Home Assistant 2026.3+: native clean-area segments
    from homeassistant.components.vacuum import Segment

    _CLEAN_AREA = VacuumEntityFeature.CLEAN_AREA
except ImportError:  # pragma: no cover - Home Assistant before 2026.3
    @dataclass(slots=True)
    class Segment:  # type: ignore[no-redef]
        """Stand-in so room cleaning can be tested before 2026.3."""

        id: str
        name: str
        group: str | None = None

    _CLEAN_AREA = VacuumEntityFeature(0)

_ACTIVITY = {
    ACTIVITY_CLEANING: VacuumActivity.CLEANING,
    ACTIVITY_DOCKED: VacuumActivity.DOCKED,
    ACTIVITY_IDLE: VacuumActivity.IDLE,
    ACTIVITY_PAUSED: VacuumActivity.PAUSED,
    ACTIVITY_RETURNING: VacuumActivity.RETURNING,
    ACTIVITY_ERROR: VacuumActivity.ERROR,
}


class Bx3Entity(CoordinatorEntity[ThingCoordinator]):
    """Shared device info and command errors for Freo S entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ThingCoordinator, key: str) -> None:
        super().__init__(coordinator)
        device_name = coordinator.config_entry.data["device_name"]
        self._attr_unique_id = f"{device_name}_{key}"
        model = coordinator.model
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_name)},
            name=model,
            manufacturer="Narwal",
            model=model,
            sw_version=coordinator.firmware,
        )

    @property
    def state_data(self) -> ThingState:
        return self.coordinator.client.state

    @property
    def available(self) -> bool:
        return super().available and self.state_data.device_reachable

    async def _run(self, action: Awaitable[Any]) -> Any:
        try:
            return await action
        except NarwalCommandError as err:
            if err.timeout:
                raise HomeAssistantError(
                    "The vacuum did not respond. It may be asleep or offline; try again."
                ) from err
            raise HomeAssistantError(str(err)) from err
        except NarwalConnectionError as err:
            raise HomeAssistantError(f"Not connected to Narwal cloud: {err}") from err


class Bx3Vacuum(Bx3Entity, StateVacuumEntity, RestoreEntity):
    """Freo S vacuum: start, pause, stop, dock, suction, and room cleaning."""

    _attr_translation_key = "narwal"
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.SEND_COMMAND
        | _CLEAN_AREA
    )
    _attr_fan_speed_list = list(SUCTION_LEVELS)

    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(coordinator, "vacuum")
        self._segments_issue_raised = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        speed = last.attributes.get("fan_speed")
        if speed in SUCTION_LEVELS and self.coordinator.suction_level is None:
            self.coordinator.suction_level = SUCTION_LEVELS[speed]

    @property
    def activity(self) -> VacuumActivity:
        return _ACTIVITY.get(self.state_data.activity, VacuumActivity.IDLE)

    @property
    def fan_speed(self) -> str | None:
        return SUCTION_BY_LEVEL.get(self.coordinator.effective_suction())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.state_data
        attrs: dict[str, Any] = {
            "status": state.status_text,
            "rooms": {room.room_id: room.display_name for room in state.rooms},
            "no_go_rooms": [],
            "suction_level": self.coordinator.effective_suction(),
            "water_level": self.coordinator.effective_water(),
            "clean_rooms": state.clean_room_ids,
            "map_id": state.map_id,
        }
        if state.current_room:
            attrs["current_room"] = state.current_room
        if state.progress is not None:
            attrs["cleaning_progress"] = state.progress
        if state.fault:
            attrs["fault"] = state.fault
        return attrs

    async def async_start(self) -> None:
        if self.state_data.activity == ACTIVITY_PAUSED:
            await self._run(self.coordinator.client.resume())
        else:
            await self._clean_rooms([room.room_id for room in self.state_data.rooms])
        await self.coordinator.async_request_refresh()

    async def async_stop(self, **kwargs: Any) -> None:
        await self._run(self.coordinator.client.stop())
        await self.coordinator.async_request_refresh()

    async def async_pause(self) -> None:
        await self._run(self.coordinator.client.pause())
        await self.coordinator.async_request_refresh()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        await self._run(self.coordinator.client.recall())
        await self.coordinator.async_request_refresh()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        level = SUCTION_LEVELS.get(fan_speed)
        if level is None:
            raise ServiceValidationError(
                f"Unknown fan speed {fan_speed!r}; use one of {list(SUCTION_LEVELS)}"
            )
        # The robot takes suction inside start_sweep. While docked there is no
        # captured property write, so the choice is kept for the next clean.
        self.coordinator.suction_level = level
        self.async_write_ha_state()

    async def async_get_segments(self) -> list[Any]:
        if not self.state_data.rooms:
            await self._run(self.coordinator.client.get_reported({"map": []}))
        return [
            Segment(id=str(room.room_id), name=room.display_name)
            for room in self.state_data.rooms
        ]

    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        await self._clean_rooms([int(segment) for segment in segment_ids])

    async def _clean_rooms(
        self,
        room_ids: list[int],
        *,
        sweep_type: int | None = None,
        suction: int | None = None,
        water: int | None = None,
        passes: int | None = None,
    ) -> None:
        if not room_ids:
            raise ServiceValidationError(
                "The robot has not reported its rooms yet. Wait for the map, then try again."
            )
        known = {room.room_id for room in self.state_data.rooms}
        unknown = [room for room in room_ids if known and room not in known]
        if unknown:
            raise ServiceValidationError(
                f"Unknown room id(s) {unknown}; known rooms: {sorted(known)}"
            )
        map_id = self.state_data.map_id
        if not map_id:
            raise ServiceValidationError("The robot has not reported its map yet.")
        await self._run(self.coordinator.client.start_sweep(
            room_ids,
            map_id=map_id,
            suction=self.coordinator.effective_suction() if suction is None else suction,
            water=self.coordinator.effective_water() if water is None else water,
            sweep_type=self.coordinator.sweep_type if sweep_type is None else sweep_type,
            passes=self.coordinator.passes if passes is None else passes,
        ))
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._check_segments_changed()
        super()._handle_coordinator_update()

    @callback
    def _check_segments_changed(self) -> None:
        if not _CLEAN_AREA or self.registry_entry is None or self._segments_issue_raised:
            return
        last_seen = self.last_seen_segments
        rooms = self.state_data.rooms
        if last_seen is None or not rooms:
            return
        current = {(str(room.room_id), room.display_name) for room in rooms}
        if current != {(segment.id, segment.name) for segment in last_seen}:
            self.async_create_segments_issue()
            self._segments_issue_raised = True

    async def async_send_command(
        self,
        command: str,
        params: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """``clean_rooms`` with optional mode, fan_speed, water_level and passes.

        ``set_schedule_enabled`` rewrites the whole schedule list with one
        entry's state changed: params ``id`` and ``enabled``.
        """
        data = params if isinstance(params, dict) else {}
        if command == "clean_rooms":
            rooms = data.get("rooms") or []
            if not rooms:
                raise ServiceValidationError("clean_rooms needs params.rooms (a list of room ids)")
            mode = data.get("mode")
            if mode is not None and mode not in SWEEP_TYPES:
                raise ServiceValidationError(
                    f"Unknown mode {mode!r}; use one of {list(SWEEP_TYPES)}"
                )
            fan = data.get("fan_speed")
            if fan is not None and fan not in SUCTION_LEVELS:
                raise ServiceValidationError(
                    f"Unknown fan_speed {fan!r}; use one of {list(SUCTION_LEVELS)}"
                )
            water = data.get("water_level")
            if water is not None and water not in WATER_LEVELS:
                raise ServiceValidationError(
                    f"Unknown water_level {water!r}; use one of {list(WATER_LEVELS)}"
                )
            await self._clean_rooms(
                [int(room) for room in rooms],
                sweep_type=SWEEP_TYPES[mode] if mode else None,
                suction=SUCTION_LEVELS[fan] if fan else None,
                water=WATER_LEVELS[water] if water else None,
                passes=int(data["passes"]) if data.get("passes") else None,
            )
        elif command == "set_schedule_enabled":
            if "id" not in data or "enabled" not in data:
                raise ServiceValidationError("set_schedule_enabled needs params.id and params.enabled")
            await self._run(self.coordinator.async_set_schedule_enabled(int(data["id"]), bool(data["enabled"])))
            self.coordinator.async_set_updated_data(self.coordinator.client.state)
        else:
            raise ServiceValidationError(
                "Unknown command; supported: clean_rooms, set_schedule_enabled"
            )


class Bx3Sensor(Bx3Entity, SensorEntity):
    """One reported ThingModel value."""

    def __init__(
        self,
        coordinator: ThingCoordinator,
        key: str,
        *,
        device_class: SensorDeviceClass | None = None,
        state_class: SensorStateClass | None = None,
        unit: str | None = None,
        suggested_unit: str | None = None,
        icon: str | None = None,
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_native_unit_of_measurement = unit
        self._attr_suggested_unit_of_measurement = suggested_unit
        self._attr_icon = icon


class Bx3BatterySensor(Bx3Sensor):
    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(
            coordinator, "battery",
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
            unit=PERCENTAGE,
        )

    @property
    def native_value(self) -> int | None:
        return self.state_data.battery


class Bx3StatusSensor(Bx3Sensor):
    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(coordinator, "status", icon="mdi:robot-vacuum")

    @property
    def native_value(self) -> str:
        return self.state_data.status_text

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.state_data
        return {
            "status_code": state.status_code,
            "activity": state.activity,
            "in_station": state.in_station,
        }


class Bx3ErrorSensor(Bx3Sensor):
    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(coordinator, "error", icon="mdi:alert-circle-outline")

    @property
    def native_value(self) -> str:
        if not self.state_data.fault:
            return "No error"
        return f"Fault {self.state_data.fault}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"code": self.state_data.fault or 0}


class Bx3DurationSensor(Bx3Sensor):
    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(
            coordinator, "elapsed_time",
            device_class=SensorDeviceClass.DURATION,
            unit=UnitOfTime.MINUTES,
            icon="mdi:timer-outline",
        )

    @property
    def native_value(self) -> int | None:
        return self.state_data.cleaning_time


class Bx3AreaSensor(Bx3Sensor):
    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(
            coordinator, "cleaned_area",
            device_class=SensorDeviceClass.AREA,
            unit=UnitOfArea.SQUARE_METERS,
        )

    @property
    def native_value(self) -> int | None:
        return self.state_data.cleaning_area


class Bx3ProgressSensor(Bx3Sensor):
    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(
            coordinator, "progress",
            unit=PERCENTAGE,
            icon="mdi:progress-check",
        )

    @property
    def native_value(self) -> int | None:
        return self.state_data.progress


class Bx3CurrentRoomSensor(Bx3Sensor):
    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(coordinator, "current_room", icon="mdi:floor-plan")

    @property
    def native_value(self) -> str | None:
        return self.state_data.current_room

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"room_ids": self.state_data.clean_room_ids}


class Bx3SchedulesSensor(Bx3Sensor):
    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(coordinator, "schedules", icon="mdi:calendar-clock")

    @property
    def native_value(self) -> int:
        return sum(1 for entry in self.state_data.schedules if entry.enabled)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "schedules": [entry.as_dict() for entry in self.state_data.schedules],
            "timing": self.state_data.raw_timing,
        }


class Bx3BinarySensor(Bx3Entity, BinarySensorEntity):
    def __init__(self, coordinator: ThingCoordinator, key: str, device_class: BinarySensorDeviceClass | None, icon: str | None) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._attr_device_class = device_class
        self._attr_icon = icon


class Bx3DockedSensor(Bx3BinarySensor):
    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(coordinator, "docked", None, "mdi:home-import-outline")

    @property
    def is_on(self) -> bool | None:
        return self.state_data.in_station


class Bx3ChargingSensor(Bx3BinarySensor):
    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(coordinator, "charging", BinarySensorDeviceClass.BATTERY_CHARGING, None)

    @property
    def is_on(self) -> bool | None:
        return self.state_data.is_charging


class Bx3ProblemSensor(Bx3BinarySensor):
    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(coordinator, "problem", BinarySensorDeviceClass.PROBLEM, None)

    @property
    def is_on(self) -> bool | None:
        if self.state_data.fault is None:
            return None
        return bool(self.state_data.fault)


class Bx3WaterSelect(Bx3Entity, SelectEntity, RestoreEntity):
    """Water level sent with the next clean. The robot has no captured live write."""

    _attr_translation_key = "water_level"
    _attr_icon = "mdi:water"
    _attr_options = list(WATER_LEVELS)

    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(coordinator, "water_level")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in WATER_LEVELS and self.coordinator.water_level is None:
            self.coordinator.water_level = WATER_LEVELS[last.state]

    @property
    def current_option(self) -> str | None:
        return WATER_BY_LEVEL.get(self.coordinator.effective_water())

    async def async_select_option(self, option: str) -> None:
        level = WATER_LEVELS.get(option)
        if level is None:
            raise ServiceValidationError(f"Unknown water level {option!r}")
        self.coordinator.water_level = level
        self.async_write_ha_state()


class Bx3CleanModeSelect(Bx3Entity, SelectEntity, RestoreEntity):
    """Clean mode sent with the next ``start_sweep``. Kept in Home Assistant."""

    _attr_translation_key = "clean_mode"
    _attr_icon = "mdi:vacuum"
    _attr_options = list(SWEEP_TYPES)

    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(coordinator, "clean_mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in SWEEP_TYPES:
            self.coordinator.sweep_type = SWEEP_TYPES[last.state]

    @property
    def current_option(self) -> str | None:
        return SWEEP_BY_TYPE.get(self.coordinator.sweep_type)

    async def async_select_option(self, option: str) -> None:
        if option not in SWEEP_TYPES:
            raise ServiceValidationError(
                f"Unknown mode {option!r}; use one of {list(SWEEP_TYPES)}"
            )
        self.coordinator.sweep_type = SWEEP_TYPES[option]
        self.async_write_ha_state()


class Bx3CleanPasses(Bx3Entity, RestoreNumber):
    """How many passes ``start_sweep`` asks for. Kept in Home Assistant."""

    _attr_translation_key = "clean_passes"
    _attr_icon = "mdi:repeat"
    _attr_native_min_value = 1
    _attr_native_max_value = 3
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: ThingCoordinator) -> None:
        super().__init__(coordinator, "clean_passes")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value:
            self.coordinator.passes = int(last.native_value)

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float:
        return float(self.coordinator.passes)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.passes = int(value)
        self.async_write_ha_state()


def async_setup_bx3_vacuum(
    hass: HomeAssistant,
    coordinator: ThingCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([Bx3Vacuum(coordinator)])


def async_setup_bx3_sensors(
    hass: HomeAssistant,
    coordinator: ThingCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([
        Bx3BatterySensor(coordinator),
        Bx3StatusSensor(coordinator),
        Bx3ErrorSensor(coordinator),
        Bx3DurationSensor(coordinator),
        Bx3AreaSensor(coordinator),
        Bx3ProgressSensor(coordinator),
        Bx3CurrentRoomSensor(coordinator),
        Bx3SchedulesSensor(coordinator),
    ])


def async_setup_bx3_binary_sensors(
    hass: HomeAssistant,
    coordinator: ThingCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([
        Bx3DockedSensor(coordinator),
        Bx3ChargingSensor(coordinator),
        Bx3ProblemSensor(coordinator),
    ])


def async_setup_bx3_selects(
    hass: HomeAssistant,
    coordinator: ThingCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([
        Bx3CleanModeSelect(coordinator),
        Bx3WaterSelect(coordinator),
    ])


def async_setup_bx3_numbers(
    hass: HomeAssistant,
    coordinator: ThingCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([Bx3CleanPasses(coordinator)])
