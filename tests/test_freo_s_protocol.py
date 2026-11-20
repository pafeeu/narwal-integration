"""ThingModel payloads and state, checked against captured Freo S traffic."""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

from custom_components.narwal.const import entry_protocol
from custom_components.narwal.narwal_client.thing_protocol import (
    ThingState,
    apply_thing_message,
    build_get_reported,
    build_invoke,
    build_set_property,
    build_start_input,
    decode_map_blob,
    encode_schedules,
    parse_timing_value,
    result_code,
    set_schedule_enabled,
    timing_inner,
    uses_thing_model,
    wrap_timing,
)

FIXTURES = Path(__file__).parent / "fixtures" / "freo_s"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _reply(name: str) -> tuple[str, dict]:
    item = _load(name)["responses"][0]
    return item["topic"], item["payload"]


def test_product_selection() -> None:
    assert uses_thing_model(product_key="ZTM5lnVgLG", thing_flag=False)
    assert uses_thing_model(product_group="BX3", thing_flag=False)
    assert uses_thing_model(product_group="bx3")
    assert uses_thing_model(product_key="NEW", thing_flag=True)
    assert not uses_thing_model(product_key="EHf6cRNRGT")
    assert not uses_thing_model(product_key="EHf6cRNRGT", thing_flag=False)
    assert entry_protocol({"product_key": "ZTM5lnVgLG"}) == "thing"
    assert entry_protocol({"product_key": "EHf6cRNRGT"}) == "protobuf"
    assert entry_protocol({"product_key": "ZTM5lnVgLG", "protocol": "protobuf"}) == "protobuf"


def test_reported_status_and_rooms() -> None:
    state = ThingState()
    apply_thing_message(state, *_reply("get_reported_robot_status.json"))
    apply_thing_message(state, *_reply("get_reported_clean_all.json"))
    apply_thing_message(state, *_reply("get_reported_map.json"))
    assert state.activity == "docked"
    assert state.status_text == "Docked"
    assert state.battery == 100
    assert state.fault == 0
    assert state.in_station is True
    assert state.is_charging is False
    assert state.suction == 2
    assert state.water == 2
    assert state.progress == 100
    assert state.cleaning_time == 3
    assert state.cleaning_area == 3
    assert [(room.room_id, room.display_name) for room in state.rooms] == [
        (1, "Korytarz"),
        (4, "Sypialnia"),
        (5, "Wejście"),
        (6, "Kuchnia"),
        (7, "Łazienka"),
        (8, "Salon"),
    ]
    assert state.map_id == 1779376970
    assert state.current_room is None  # docked, the task is not running
    assert len(state.schedules) == 9
    assert sum(entry.enabled for entry in state.schedules) == 2


def test_status_push_sequence_matches_the_capture() -> None:
    state = ThingState()
    apply_thing_message(state, *_reply("get_reported_robot_status.json"))
    seen: list[tuple[int | None, str]] = []
    for item in _load("pushes_status.json"):
        before = state.status_code
        apply_thing_message(state, item["topic"], item["payload"])
        if state.status_code != before and state.status_code is not None:
            seen.append((state.status_code, state.activity))
    assert seen == [
        (4, "cleaning"),
        (3, "paused"),
        (2, "idle"),
        (5, "returning"),
        (6, "docked"),
    ]
    assert state.in_station is True
    cleaning = ThingState()
    # The cleaning push names room 5 before the robot leaves the station.
    first = _load("pushes_status.json")[1]
    apply_thing_message(cleaning, first["topic"], first["payload"])
    apply_thing_message(cleaning, *_reply("get_reported_map.json"))
    assert cleaning.clean_room_ids == [5]
    assert cleaning.current_room == "Wejście"
    assert cleaning.activity == "cleaning"


def test_command_payloads_match_fixtures() -> None:
    start = _load("start_sweep_room5.json")["request"]["payload"]
    built = build_invoke("mid", "clean", "start_sweep", build_start_input(room_ids=[5], map_id=1779376970))
    assert built["input"] == start["input"]
    assert built["module"] == "clean"
    assert built["actionIdentifier"] == "start_sweep"

    for name, action, module in (
        ("pause_sweeping.json", "pause_sweeping", "clean"),
        ("recall_station.json", "recall_station", "clean"),
        ("heartbeat.json", "heartbeat", "custom"),
    ):
        payload = _load(name)["request"]["payload"]
        got = build_invoke("mid", module, action, {})
        assert got["actionIdentifier"] == payload["actionIdentifier"]
        assert got["module"] == payload["module"]
        assert got["input"] == payload["input"]
        assert got["method"] == "invoke"

    reported = _load("get_reported_robot_status.json")["request"]["payload"]
    assert build_get_reported("mid", {"robot_status": []})["properties"] == reported["properties"]
    timing_read = _load("get_reported_clean_timing.json")["request"]["payload"]
    assert build_get_reported("mid", {"clean": ["timing"]})["properties"] == timing_read["properties"]


def test_schedule_rewrite_matches_the_captured_set() -> None:
    original = timing_inner(
        _load("get_reported_clean_all.json")["responses"][0]["payload"]["payload"]["clean"]["timing"]["value"]
    )
    assert encode_schedules(parse_timing_value(original)) == original

    disabled = _load("set_timing_disable_id10.json")["request"]["payload"]["property"]["clean"]["timing"]["value"]
    updated = set_schedule_enabled(parse_timing_value(original), 10, False)
    assert updated == timing_inner(disabled)
    assert wrap_timing(updated) == disabled

    restored = _load("set_timing_restore_id10.json")["request"]["payload"]["property"]["clean"]["timing"]["value"]
    again = set_schedule_enabled(parse_timing_value(updated), 10, True)
    assert again == original
    assert wrap_timing(again) == restored

    body = build_set_property("mid", {"clean": {"timing": {"value": disabled}}})
    assert body["method"] == "set"
    assert body["property"]["clean"]["timing"]["value"] == disabled
    one_shot = next(entry for entry in parse_timing_value(original) if entry.id == 8)
    assert one_shot.time_label == "10:30 10/3"
    assert one_shot.days_label == "once"
    weekly = next(entry for entry in parse_timing_value(original) if entry.id == 10)
    assert weekly.days_label == "Tue, Thu"
    assert weekly.time_label == "15:50"


def test_map_blob_is_gzip() -> None:
    reply = _load("get_current_map.json")["responses"][1]["payload"]
    assert result_code(reply) == 0
    assert decode_map_blob(reply["output"]["current_map"]) == b"freo-s-map"
    # The helper accepts the same bytes the capture stored: standard gzip+base64.
    assert gzip.decompress(base64.b64decode(reply["output"]["current_map"])) == b"freo-s-map"


def test_replies_are_matched_by_message_id() -> None:
    from types import SimpleNamespace

    from custom_components.narwal.narwal_client.thing_client import ThingClient

    client = ThingClient(
        product_key="ZTM5lnVgLG",
        device_name="a" * 32,
        user_uuid="b" * 32,
        mqtt_password="token",
    )
    import threading

    event = threading.Event()
    client._pending["wanted"] = {"event": event, "kind": "action", "reply": None}

    def deliver(message_id: str, topic: str) -> None:
        payload = json.dumps({"code": 0, "method": "invoke", "messageId": message_id}).encode()
        client._on_message(None, None, SimpleNamespace(topic=topic, payload=payload))

    deliver("other", f"{client.base}/action/invoke/reply")
    assert not event.is_set()
    deliver("wanted", f"{client.base}/action/request/reply")
    assert not event.is_set()  # cloud ack with code 0 is not the robot result
    deliver("wanted", f"{client.base}/action/invoke/reply")
    assert event.is_set()
    assert client._pending["wanted"]["reply"]["messageId"] == "wanted"
