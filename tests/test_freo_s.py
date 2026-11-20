"""Freo S entities against captured ThingModel payloads. No network."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.vacuum import VacuumActivity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_get_all_descriptions
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.const import DOMAIN
from custom_components.narwal.narwal_client.thing_client import ThingClient
from custom_components.narwal.narwal_client.thing_protocol import (
    delete_schedule_id,
    once_timestamp,
    parse_timing_value,
    upsert_one_time,
)

from .conftest import FakeThing

FIXTURES = Path(__file__).parent / "fixtures" / "freo_s"
DEVICE = "fedcba0987654321fedcba0987654321"
PRODUCT = "ZTM5lnVgLG"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _entity(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{DEVICE}_{key}")
    assert entity_id, f"no {domain} entity for {key}"
    return entity_id


@pytest.fixture
async def freo(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=f"{PRODUCT}_{DEVICE}",
        data={
            "email": "user@example.com",
            "password": "secret",
            "region": "us",
            "product_key": PRODUCT,
            "device_name": DEVICE,
            "user_uuid": "0" * 32,
            "access_token": "not.a.jwt",
            "refresh_token": "refresh",
            "protocol": "thing",
            "product_group": "BX3",
            "model": "Narwal Freo S",
        },
    )
    thing = FakeThing()

    async def fake_connect(self: ThingClient) -> None:
        self._loop = asyncio.get_running_loop()
        self._client = SimpleNamespace()
        self._connected.set()
        self.state.device_reachable = True

    async def fake_disconnect(self: ThingClient) -> None:
        self._connected.clear()

    def fake_request_blocking(self: ThingClient, suffix: str, body: dict, kind: str, timeout: float):
        return thing.answer(self, suffix, body, kind)

    session = SimpleNamespace(access_token="new.token", refresh_token="new-refresh")
    with (
        patch.object(ThingClient, "connect", fake_connect),
        patch.object(ThingClient, "disconnect", fake_disconnect),
        patch.object(ThingClient, "_request_blocking", fake_request_blocking),
        patch("custom_components.narwal.thing_coordinator.NarwalCloud.refresh_token", return_value=session),
        patch("custom_components.narwal._register_card"),
    ):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        yield SimpleNamespace(entry=entry, calls=thing.calls, coordinator=entry.runtime_data)
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def _invokes(freo, action: str) -> list[dict]:
    return [call[3] for call in freo.calls if call[0] == "invoke" and call[2] == action]


async def test_freo_s_states(hass: HomeAssistant, freo) -> None:
    vacuum = _entity(hass, "vacuum", "vacuum")
    state = hass.states.get(vacuum)
    assert state.state == VacuumActivity.DOCKED
    assert state.attributes["fan_speed"] == "Normal"
    assert state.attributes["status"] == "Docked"
    assert state.attributes["rooms"][5] == "Wejście"
    assert state.attributes["map_id"] == 1779376970
    assert hass.states.get(_entity(hass, "sensor", "battery")).state == "100"
    assert hass.states.get(_entity(hass, "sensor", "error")).state == "no_error"
    assert hass.states.get(_entity(hass, "sensor", "schedules")).state == "2"
    assert hass.states.get(_entity(hass, "binary_sensor", "docked")).state == "on"
    assert hass.states.get(_entity(hass, "binary_sensor", "charging")).state == "off"
    assert hass.states.get(_entity(hass, "binary_sensor", "problem")).state == "off"
    assert hass.states.get(_entity(hass, "select", "water_level")).state == "medium"
    assert hass.states.get(_entity(hass, "select", "clean_mode")).state == "Vacuum Only"
    assert state.attributes["suction_applies_to"] == "next_clean"
    assert hass.states.get(_entity(hass, "sensor", "elapsed_time")).state == "3"
    assert hass.states.get(_entity(hass, "sensor", "cleaned_area")).state == "3"
    # Protobuf-only entities stay off this device.
    assert er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{DEVICE}_wash_mop") is None
    assert er.async_get(hass).async_get_entity_id("camera", DOMAIN, f"{DEVICE}_map") is None
    assert freo.coordinator.protocol == "thing"
    assert freo.coordinator.model == "Narwal Freo S"
    # Setup reads the map before entities are published, so rooms are not empty.
    setup_reads = [payload for kind, payload in freo.calls if kind == "get"]
    assert {"map": []} in setup_reads
    assert state.attributes["rooms"]


async def test_start_pause_stop_and_dock(hass: HomeAssistant, freo) -> None:
    vacuum = _entity(hass, "vacuum", "vacuum")
    await hass.services.async_call("vacuum", "start", {"entity_id": vacuum}, blocking=True)
    started = _invokes(freo, "start_sweep")[-1]
    assert started["area_ids"] == [1, 4, 5, 6, 7, 8]
    assert started["map_id"] == 1779376970
    assert started["sweep_type"] == 0
    assert started["suction_level"] == 2
    assert started["water_level"] == 2

    await hass.services.async_call("vacuum", "pause", {"entity_id": vacuum}, blocking=True)
    await hass.services.async_call("vacuum", "stop", {"entity_id": vacuum}, blocking=True)
    await hass.services.async_call("vacuum", "return_to_base", {"entity_id": vacuum}, blocking=True)
    assert _invokes(freo, "pause_sweeping") == [{}]
    assert _invokes(freo, "stop_sweeping") == [{}]
    assert _invokes(freo, "recall_station") == [{}]


async def test_room_clean_uses_selected_levels(hass: HomeAssistant, freo) -> None:
    vacuum = _entity(hass, "vacuum", "vacuum")
    await hass.services.async_call(
        "vacuum", "set_fan_speed", {"entity_id": vacuum, "fan_speed": "Strong"}, blocking=True
    )
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": _entity(hass, "select", "water_level"), "option": "high"},
        blocking=True,
    )
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": _entity(hass, "select", "clean_mode"), "option": "Vacuum Only"},
        blocking=True,
    )
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _entity(hass, "number", "clean_passes"), "value": 2},
        blocking=True,
    )
    await hass.services.async_call(
        "vacuum", "send_command",
        {"entity_id": vacuum, "command": "clean_rooms", "params": {"rooms": [5]}},
        blocking=True,
    )
    payload = _invokes(freo, "start_sweep")[-1]
    assert payload == {
        "sweep_mode": 1,
        "sweep_type": 0,
        "sweep_times": 2,
        "mop_times": 2,
        "suction_level": 3,
        "water_level": 3,
        "map_id": 1779376970,
        "area_ids": [5],
    }
    # Fan speed is remembered for the next clean; nothing is written while docked.
    assert not any(call[0] == "set" for call in freo.calls)


async def test_clean_segments_and_unknown_room(hass: HomeAssistant, freo) -> None:
    vacuum_id = _entity(hass, "vacuum", "vacuum")
    vacuum = hass.data["vacuum"].get_entity(vacuum_id)
    segments = await vacuum.async_get_segments()
    assert [(segment.id, segment.name) for segment in segments] == [
        ("1", "Korytarz"),
        ("4", "Sypialnia"),
        ("5", "Wejście"),
        ("6", "Kuchnia"),
        ("7", "Łazienka"),
        ("8", "Salon"),
    ]
    await vacuum.async_clean_segments(["6", "8"])
    assert _invokes(freo, "start_sweep")[-1]["area_ids"] == [6, 8]
    with pytest.raises(ServiceValidationError):
        await vacuum.async_clean_segments(["2"])


async def test_resume_uses_continue_sweep(hass: HomeAssistant, freo) -> None:
    freo.coordinator.client.state.robot_status["status"] = 3
    vacuum = _entity(hass, "vacuum", "vacuum")
    await hass.services.async_call("vacuum", "start", {"entity_id": vacuum}, blocking=True)
    assert _invokes(freo, "continue_sweep") == [{}]
    assert not _invokes(freo, "start_sweep")


async def test_schedule_enable_rewrites_the_whole_list(hass: HomeAssistant, freo) -> None:
    vacuum = _entity(hass, "vacuum", "vacuum")
    await hass.services.async_call(
        "narwal", "set_schedules",
        {"entity_id": vacuum, "schedule_id": 10, "enabled": False},
        blocking=True,
    )
    expected = _load("set_timing_disable_id10.json")["request"]["payload"]["property"]["clean"]["timing"]["value"]
    written = [call for call in freo.calls if call[0] == "set"]
    assert len(written) == 1
    assert written[0][1]["clean"]["timing"]["value"] == expected
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "sensor", "schedules")).state == "1"

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "narwal", "set_schedules",
            {"entity_id": vacuum, "schedule_id": 99, "enabled": True},
            blocking=True,
        )
    assert len([call for call in freo.calls if call[0] == "set"]) == 1


async def test_property_push_updates_the_vacuum(hass: HomeAssistant, freo) -> None:
    push = _load("pushes_status.json")[1]
    client = freo.coordinator.client
    topic = f"/{PRODUCT}/{DEVICE}/thing/{push['topic'].removeprefix('thing/')}"
    client._on_message(None, None, SimpleNamespace(topic=topic, payload=json.dumps(push["payload"]).encode()))
    await hass.async_block_till_done()
    state = hass.states.get(_entity(hass, "vacuum", "vacuum"))
    assert state.state == VacuumActivity.CLEANING
    assert state.attributes["current_room"] == "Wejście"
    assert hass.states.get(_entity(hass, "sensor", "current_room")).state == "Wejście"


async def test_status_6_is_docked_and_current_room_is_a_name(hass: HomeAssistant, freo) -> None:
    client = freo.coordinator.client
    root = f"/{PRODUCT}/{DEVICE}/thing"

    def push(payload: dict) -> None:
        client._on_message(None, None, SimpleNamespace(
            topic=f"{root}/property/down", payload=json.dumps(payload).encode(),
        ))

    push({"method": "reported", "messageId": "room", "property": {
        "clean": {"clean_rooms": {"time": 1, "value": [6]}},
        "robot_status": {"status": {"time": 1, "value": 4}},
    }})
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "sensor", "current_room")).state == "Kuchnia"
    assert hass.states.get(_entity(hass, "vacuum", "vacuum")).attributes["current_room"] == "Kuchnia"

    push({"method": "reported", "messageId": "dock", "property": {
        "robot_status": {
            "status": {"time": 2, "value": 6},
            "robot_in_station": {"time": 2, "value": True},
            "battery_percentage": {"time": 2, "value": 98},
        },
        "clean": {"clean_rooms": {"time": 2, "value": []}},
    }})
    await hass.async_block_till_done()
    vacuum = hass.states.get(_entity(hass, "vacuum", "vacuum"))
    assert vacuum.state == VacuumActivity.DOCKED
    assert vacuum.attributes["status"] == "Working at station"
    assert hass.states.get(_entity(hass, "binary_sensor", "charging")).state == "on"
    assert hass.states.get(_entity(hass, "sensor", "current_room")).state == "unknown"


async def test_one_time_schedule_reuses_a_disabled_entry(hass: HomeAssistant, freo) -> None:
    vacuum = _entity(hass, "vacuum", "vacuum")
    await hass.services.async_call(
        "narwal", "schedule_clean_once",
        {"entity_id": vacuum, "date": "2026-10-03", "time": "10:30", "rooms": ["Kuchnia", 1]},
        blocking=True,
    )
    written = [call for call in freo.calls if call[0] == "set"][-1][1]
    inner = json.loads(written["clean"]["timing"]["value"])["time"]
    entries = parse_timing_value(inner)
    reused = next(entry for entry in entries if entry.id == 8)
    assert reused.state == 1
    assert reused.repeat == 0
    assert reused.days == "0000000"
    assert reused.time == str(once_timestamp("2026-10-03", "10:30"))
    assert reused.rooms == [6, 1]
    assert reused.sweep_type == 0
    assert any(entry.id == 9 and entry.state == 0 for entry in entries)

    await hass.services.async_call(
        "narwal", "delete_schedule",
        {"entity_id": vacuum, "schedule_id": 10},
        blocking=True,
    )
    deleted = json.loads([call for call in freo.calls if call[0] == "set"][-1][1]["clean"]["timing"]["value"])["time"]
    assert all(entry.id != 10 for entry in parse_timing_value(deleted))
    await hass.async_block_till_done()
    lines = hass.states.get(_entity(hass, "sensor", "schedules")).attributes["lines"]
    assert any(str(line).startswith("8 ") for line in lines)


async def test_unknown_fault_keeps_the_raw_code(hass: HomeAssistant, freo) -> None:
    client = freo.coordinator.client
    client.state.robot_status["fault"] = 7
    freo.coordinator.async_set_updated_data(client.state)
    await hass.async_block_till_done()
    error = hass.states.get(_entity(hass, "sensor", "error"))
    assert error.state == "unknown_fault"
    assert error.attributes["code"] == 7


async def test_every_registered_service_has_a_services_yaml_entry(
    hass: HomeAssistant, freo, caplog: pytest.LogCaptureFixture,
) -> None:
    """Home Assistant logs an error when a domain registers services without services.yaml."""
    caplog.set_level(logging.ERROR)
    descriptions = await async_get_all_descriptions(hass)
    assert "Failed to load services.yaml for integration: narwal" not in caplog.text
    registered = set(hass.services.async_services()[DOMAIN])
    assert registered == {"set_schedules", "schedule_clean_once", "delete_schedule"}
    assert registered <= set(descriptions["narwal"])
    once = descriptions["narwal"]["schedule_clean_once"]["fields"]
    assert once["date"]["selector"]["date"] == {}
    assert once["time"]["selector"]["time"] == {}
    assert once["rooms"]["selector"]["text"]["multiple"] is True
    assert once["areas"]["selector"]["area"]["multiple"] is True
    assert once["mode"]["selector"]["select"]["options"] == ["Vacuum Only", "Vacuum & Mop"]
    assert once["suction"]["selector"]["select"]["options"] == ["Quiet", "Normal", "Strong", "Max"]
    assert once["water"]["selector"]["select"]["options"] == ["low", "medium", "high"]
    assert once["passes"]["selector"]["number"]["min"] == 1
    assert once["passes"]["selector"]["number"]["max"] == 2
    for name in registered:
        assert descriptions["narwal"][name]["fields"]
