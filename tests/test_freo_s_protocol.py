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
    localized_status,
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


def test_live_script_does_not_import_homeassistant() -> None:
    """The CLI loads narwal_client without the integration package."""
    import os
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts" / "freo_s_live.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        check=False,
        capture_output=True,
        text=True,
        cwd=script.parents[1],
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    probe = (
        "import runpy, sys\n"
        "sys.argv = ['freo_s_live.py', '--help']\n"
        "try:\n"
        f"    runpy.run_path({str(script)!r}, run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "raise SystemExit(1 if 'homeassistant' in sys.modules else 0)\n"
    )
    imported = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert imported.returncode == 0, imported.stderr or imported.stdout


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
    # No pose or current-zone field exists in the pushes. Without the map
    # room list, an id is not presented as the room name.
    unnamed = ThingState()
    apply_thing_message(unnamed, first["topic"], first["payload"])
    assert unnamed.clean_room_ids == [5]
    assert unnamed.current_room is None
    unnamed.robot_status["status"] = 6
    unnamed.robot_status["robot_in_station"] = True
    assert unnamed.activity == "docked"
    assert unnamed.status_text == "Working at station"
    assert localized_status(unnamed, "pl") == "Praca w stacji"
    cleaning.rooms = cleaning.rooms  # map already loaded above
    assert localized_status(cleaning, "en") == "Cleaning: Wejście"
    assert localized_status(cleaning, "pl") == "Sprzątanie: Wejście"
    cleaning.robot_status["status"] = 3
    assert localized_status(cleaning, "pl") == "Pauza: Wejście"
    cleaning.robot_status["status"] = 5
    cleaning.clean["clean_rooms"] = []
    assert localized_status(cleaning, "pl") == "Powrót do stacji"
    assert localized_status(cleaning, "de") == "Returning to dock"  # unknown -> English


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


def test_one_time_schedule_overwrites_the_first_disabled_once_row() -> None:
    from custom_components.narwal.narwal_client.thing_protocol import (
        delete_schedule_id,
        once_timestamp,
        upsert_one_time,
    )

    original = timing_inner(
        _load("get_reported_clean_all.json")["responses"][0]["payload"]["payload"]["clean"]["timing"]["value"]
    )
    # UTC printout of the captured epochs. The app's displayed clock is not in the capture.
    assert once_timestamp("2026-10-03", "10:30") == 1791023400
    assert once_timestamp("2026-10-03", "10:30:00") == 1791023400
    assert once_timestamp("2026-09-21", "16:45") == 1790009100
    updated = upsert_one_time(
        parse_timing_value(original),
        when=once_timestamp("2026-10-03", "10:30"),
        rooms=[6],
        map_id=1779376970,
        suction=2,
        water=2,
        sweep_type=0,
        passes=1,
    )
    reused = next(entry for entry in parse_timing_value(updated) if entry.id == 8)
    assert reused.state == 1
    assert reused.time_label == "10:30 10/3"
    assert reused.rooms == [6]
    kept = delete_schedule_id(parse_timing_value(updated), 11)
    assert all(entry.id != 11 for entry in parse_timing_value(kept))
    assert encode_schedules(parse_timing_value(kept)) == kept


def test_service_translations_cover_every_services_yaml_field() -> None:
    import yaml

    root = Path(__file__).parent.parent / "custom_components" / "narwal"
    services = yaml.safe_load((root / "services.yaml").read_text())
    assert services
    for filename in ("strings.json", "translations/en.json", "translations/pl.json"):
        translated = json.loads((root / filename).read_text())["services"]
        for service, spec in services.items():
            entry = translated[service]
            assert entry["name"]
            assert entry["description"]
            for field in spec["fields"]:
                assert translated[service]["fields"][field]["name"]
                assert translated[service]["fields"][field]["description"]


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


def test_publish_and_subscribe_topics() -> None:
    """The broker topic is /<product>/<device>/thing/<path>, with thing/ exactly once."""
    from custom_components.narwal.narwal_client.thing_client import ThingClient

    product = "ZTM5lnVgLG"
    device = "a" * 32
    client = ThingClient(
        product_key=product,
        device_name=device,
        user_uuid="b" * 32,
        mqtt_password="token",
    )
    root = f"/{product}/{device}/thing"
    subscribed: list[str] = []
    published: list[str] = []

    class Broker:
        def subscribe(self, topic: str, qos: int = 0) -> tuple[int, int]:
            subscribed.append(topic)
            return (0, 1)

        def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
            published.append(topic)
            body = json.loads(payload)
            slot = client._pending[body["messageId"]]
            slot["reply"] = {"method": "getReported", "messageId": body["messageId"], "payload": {"code": 0}}
            slot["event"].set()

    client._on_connect(Broker(), None, None, 0)
    assert subscribed == [
        f"{root}/property/up/reply",
        f"{root}/property/down",
        f"{root}/action/request/reply",
        f"{root}/action/invoke/reply",
        f"{root}/event/down",
    ]
    client._client = Broker()
    client._connected.set()
    client._request_blocking(
        "property/up",
        {"method": "getReported", "messageId": "mid-1", "properties": {"robot_status": []}},
        "property",
        1.0,
    )
    client._request_blocking(
        "action/request",
        {"method": "invoke", "actionIdentifier": "recall_station", "messageId": "mid-2", "module": "clean", "input": {}},
        "action",
        1.0,
    )
    assert published == [
        f"{root}/property/up",
        f"{root}/action/request",
    ]
    for topic in subscribed + published:
        assert topic.count("/thing/") == 1
        assert "/thing/thing/" not in topic


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

    deliver("other", client.topic("action/invoke/reply"))
    assert not event.is_set()
    deliver("wanted", client.topic("action/request/reply"))
    assert not event.is_set()  # cloud ack with code 0 is not the robot result
    deliver("wanted", client.topic("action/invoke/reply"))
    assert event.is_set()
    assert client._pending["wanted"]["reply"]["messageId"] == "wanted"
