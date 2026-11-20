"""Base entity for Narwal integration."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NarwalCoordinator
from .narwal_client import NarwalCommandError, NarwalConnectionError, NarwalState
from .narwal_client.thing_protocol import ThingState
from .thing_coordinator import ThingCoordinator


class NarwalEntity(CoordinatorEntity[NarwalCoordinator]):
    """Base class for Narwal entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NarwalCoordinator, key: str | None = None) -> None:
        super().__init__(coordinator)
        data = coordinator.config_entry.data
        self._device_name = data["device_name"]
        if key:
            self._attr_unique_id = f"{self._device_name}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, data["device_name"])},
            name=f"Narwal {data['product_key']}",
            manufacturer="Narwal",
            model="Freo X Ultra",
            sw_version=coordinator.client.state.firmware_version or None,
        )

    @property
    def state_data(self) -> NarwalState:
        return self.coordinator.client.state

    @property
    def available(self) -> bool:
        return super().available and self.state_data.device_reachable

    async def _run(self, action: Awaitable[Any]) -> Any:
        """Await a client command; surface failures as HomeAssistantError."""
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


class Bx3Entity(CoordinatorEntity[ThingCoordinator]):
    """Shared device info and command errors for a Freo S (ThingModel) entity."""

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
