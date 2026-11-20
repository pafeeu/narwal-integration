"""Binary sensor entities for Narwal robot vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import PROTOCOL_THING
from .coordinator import NarwalCoordinator
from .entity import Bx3Entity, NarwalEntity
from .narwal_client import NarwalState


@dataclass(frozen=True, kw_only=True)
class NarwalBinaryDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[NarwalState], bool | None]


BINARY_SENSORS: tuple[NarwalBinaryDescription, ...] = (
    NarwalBinaryDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        is_on_fn=lambda s: s.is_charging if s.base else None,
    ),
    NarwalBinaryDescription(
        key="docked",
        translation_key="docked",
        icon="mdi:home-import-outline",
        is_on_fn=lambda s: s.on_station if s.base else None,
    ),
    NarwalBinaryDescription(
        key="problem",
        translation_key="problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda s: bool(s.error_codes) if s.base else None,
    ),
    NarwalBinaryDescription(
        key="maintenance_due",
        translation_key="maintenance_due",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:wrench-clock",
        is_on_fn=lambda s: bool(s.consumables_due.get("maintain") or s.consumables_due.get("replace")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if entry.runtime_data.protocol == PROTOCOL_THING:
        coordinator = entry.runtime_data
        async_add_entities([
            Bx3DockedSensor(coordinator),
            Bx3ChargingSensor(coordinator),
            Bx3ProblemSensor(coordinator),
        ])
        return
    coordinator = entry.runtime_data
    async_add_entities(NarwalBinarySensor(coordinator, d) for d in BINARY_SENSORS)


class NarwalBinarySensor(NarwalEntity, BinarySensorEntity):
    entity_description: NarwalBinaryDescription

    def __init__(self, coordinator: NarwalCoordinator, description: NarwalBinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.state_data)

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


