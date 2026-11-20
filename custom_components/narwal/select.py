"""Select entities for Narwal robot vacuum."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NarwalConfigEntry
from .const import CLEAN_MODE_LIST, CLEAN_MODE_MAP, CLEAN_MODE_REVERSE, MOP_HUMIDITY_MAP, PROTOCOL_THING
from .coordinator import NarwalCoordinator
from .entity import Bx3Entity, NarwalEntity
from .narwal_client.thing_protocol import SWEEP_BY_TYPE, SWEEP_TYPES, WATER_BY_LEVEL, WATER_LEVELS


@dataclass(frozen=True, kw_only=True)
class NarwalConfigSelectDescription(SelectEntityDescription):
    config_key: str
    # HA option -> enum label sent to / read from the robot.
    values: dict[str, str]


CONFIG_SELECTS: tuple[NarwalConfigSelectDescription, ...] = (
    NarwalConfigSelectDescription(
        key="mop_dry_strength",
        translation_key="mop_dry_strength",
        icon="mdi:fan",
        config_key="dryMopStrength",
        values={
            "quiet": "DRY_MOP_STRENGTH_QUIET",
            "strong": "DRY_MOP_STRENGTH_STRONG",
            "smart": "DRY_MOP_STRENGTH_SMART",
        },
    ),
    NarwalConfigSelectDescription(
        key="mop_wash_frequency",
        translation_key="mop_wash_frequency",
        icon="mdi:washing-machine",
        config_key="cleanMopFreq",
        values={
            "efficient": "CLEAN_MOP_FREQ_EFFICIENT",
            "normal": "CLEAN_MOP_FREQ_NORMAL",
            "deep": "CLEAN_MOP_FREQ_DEEP",
        },
    ),
    NarwalConfigSelectDescription(
        key="carpet_strategy",
        translation_key="carpet_strategy",
        icon="mdi:rug",
        config_key="carpetCleanOption",
        values={
            "cross": "CARPET_CLEAN_OPTION_ACROSS",
            "boost": "CARPET_CLEAN_OPTION_PRESSURE",
            "ignore": "CARPET_CLEAN_OPTION_IGNORE",
            "avoid": "CARPET_CLEAN_OPTION_AVOID",
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if entry.runtime_data.protocol == PROTOCOL_THING:
        coordinator = entry.runtime_data
        async_add_entities([Bx3CleanModeSelect(coordinator), Bx3WaterSelect(coordinator)])
        return
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = [
        NarwalCleanModeSelect(coordinator),
        NarwalMopHumiditySelect(coordinator),
    ]
    entities += [NarwalConfigSelect(coordinator, d) for d in CONFIG_SELECTS]
    async_add_entities(entities)


class NarwalCleanModeSelect(NarwalEntity, SelectEntity, RestoreEntity):
    """Clean mode used by vacuum.start and room cleaning (kept in HA)."""

    _attr_translation_key = "clean_mode"
    _attr_icon = "mdi:spray-bottle"
    _attr_options = CLEAN_MODE_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "clean_mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in CLEAN_MODE_MAP:
            self.coordinator.selected_clean_mode = CLEAN_MODE_MAP[last.state]

    @property
    def available(self) -> bool:
        return True  # local preference; usable even while the robot sleeps

    @property
    def current_option(self) -> str:
        return CLEAN_MODE_REVERSE[self.coordinator.selected_clean_mode]

    async def async_select_option(self, option: str) -> None:
        self.coordinator.selected_clean_mode = CLEAN_MODE_MAP[option]
        self.async_write_ha_state()


class NarwalMopHumiditySelect(NarwalEntity, SelectEntity):
    """Mop wetness, as reported by the robot."""

    _attr_translation_key = "mop_humidity"
    _attr_icon = "mdi:water-percent"
    _attr_options = list(MOP_HUMIDITY_MAP)

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "mop_humidity")

    @property
    def current_option(self) -> str | None:
        level = self.state_data.mop_humidity
        return level.name.lower() if level else None

    async def async_select_option(self, option: str) -> None:
        await self._run(self.coordinator.client.set_mop_humidity(MOP_HUMIDITY_MAP[option]))
        self.async_write_ha_state()


class NarwalConfigSelect(NarwalEntity, SelectEntity):
    """An enum-valued robot setting."""

    entity_description: NarwalConfigSelectDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NarwalCoordinator, description: NarwalConfigSelectDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_options = list(description.values)

    @property
    def available(self) -> bool:
        return super().available and bool(self.state_data.config)

    @property
    def current_option(self) -> str | None:
        label = self.state_data.config.get(self.entity_description.config_key)
        return next((k for k, v in self.entity_description.values.items() if v == label), None)

    async def async_select_option(self, option: str) -> None:
        d = self.entity_description
        await self._run(self.coordinator.client.set_config(**{d.config_key: d.values[option]}))
        self.async_write_ha_state()

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


