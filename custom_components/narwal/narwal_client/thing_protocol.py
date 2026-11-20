"""ThingModel JSON protocol used by the Narwal Freo S (product group BX3).

The Freo X Ultra and similar robots speak protobuf on ``/<product>/<device>/<command>``.
The Freo S never answers those topics. Its app uses plain JSON on
``/<productKey>/<deviceId>/thing/...``:

* read properties: ``{"method":"getReported",...}`` on ``thing/property/up``,
  reply on ``thing/property/up/reply``
* actions: ``{"method":"invoke","actionIdentifier":...,"module":...,"input":{...}}``
  on ``thing/action/request``, acks on ``thing/action/request/reply`` (cloud)
  and ``thing/action/invoke/reply`` (robot)
* pushes: ``thing/property/down`` and ``thing/event/down``
* schedules: the whole ``clean.timing`` string, written with ``method`` ``set``

Field values below come from captured app traffic unless the name says otherwise.
"""

from __future__ import annotations

import base64
import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import (
    ACTIVITY_CLEANING,
    ACTIVITY_DOCKED,
    ACTIVITY_ERROR,
    ACTIVITY_IDLE,
    ACTIVITY_PAUSED,
    ACTIVITY_RETURNING,
)

PROTOCOL_PROTOBUF = "protobuf"
PROTOCOL_THING = "thing"

# product key -> product group. ZTM5lnVgLG is the Freo S.
THING_PRODUCTS: dict[str, str] = {
    "ZTM5lnVgLG": "BX3",
}
THING_GROUPS = frozenset({"BX3"})

# Suction sent as ``suction_level``. 2 is the value captured in start_sweep.
# 3 is stored in schedules on the same robot. 1 and 4 are the neighbouring
# steps of that scale (Quiet / Max) and were not in the capture.
SUCTION_LEVELS: dict[str, int] = {
    "Quiet": 1,
    "Normal": 2,
    "Strong": 3,
    "Max": 4,
}
SUCTION_BY_LEVEL = {level: name for name, level in SUCTION_LEVELS.items()}

# Water sent as ``water_level``. 2 is the captured start_sweep value.
WATER_LEVELS: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
}
WATER_BY_LEVEL = {level: name for name, level in WATER_LEVELS.items()}

# sweep_type 2 was sent by the app for vacuum+mop. 0 is what the robot reports
# while idle and what stored schedules use in the field the app treats as mode.
SWEEP_TYPES: dict[str, int] = {
    "Vacuum & Mop": 2,
    "Vacuum Only": 0,
}
SWEEP_BY_TYPE = {value: name for name, value in SWEEP_TYPES.items()}

# robot_status.status values seen in one capture, in the order they happened:
# 9 docked idle, 4 cleaning, 3 paused, 2 task just cleared, 5 returning, 6 on
# the station doing post-clean work, then back toward idle.
STATUS_ACTIVITY = {
    2: ACTIVITY_IDLE,
    3: ACTIVITY_PAUSED,
    4: ACTIVITY_CLEANING,
    5: ACTIVITY_RETURNING,
    6: ACTIVITY_DOCKED,
    9: ACTIVITY_DOCKED,
}
STATUS_TEXT = {
    2: "Idle",
    3: "Paused",
    4: "Cleaning",
    5: "Returning",
    6: "Working at station",
    9: "Docked",
}

_DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
_ACTIVE_STATUSES = frozenset({3, 4, 5})


def uses_thing_model(
    *,
    product_key: str = "",
    product_group: str = "",
    thing_flag: bool | None = None,
) -> bool:
    """True when this robot should use the ThingModel path.

    A known Freo S product key or group wins even when the cloud flag
    ``switch_to_robothings`` is missing or false: that flag was false on the
    captured Freo S, and the robot still spoke only ThingModel. An explicit
    true flag selects ThingModel for a product we have not listed yet.
    """
    if product_key in THING_PRODUCTS:
        return True
    if product_group.upper() in THING_GROUPS:
        return True
    return thing_flag is True


def model_name(product_key: str = "", product_group: str = "", reported: str = "") -> str:
    """Human-readable model. ``reported`` is the cloud's productModel, if any."""
    if reported:
        return reported
    if product_key in THING_PRODUCTS or product_group.upper() in THING_GROUPS:
        return "Narwal Freo S"
    return ""


def thing_suffix(topic: str) -> str:
    """``thing/property/down`` from a full or relative MQTT topic."""
    if "/thing/" in topic:
        return topic.split("/thing/", 1)[1]
    if topic.startswith("thing/"):
        return topic[len("thing/"):]
    return topic


def build_get_reported(message_id: str, properties: dict[str, list[str]]) -> dict[str, Any]:
    return {"method": "getReported", "messageId": message_id, "properties": properties}


def build_invoke(message_id: str, module: str, action: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "method": "invoke",
        "actionIdentifier": action,
        "messageId": message_id,
        "module": module,
        "input": data or {},
    }


def build_set_property(message_id: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"method": "set", "messageId": message_id, "property": properties}


def build_start_input(
    *,
    room_ids: list[int],
    map_id: int,
    suction: int = 2,
    water: int = 2,
    sweep_type: int = 2,
    passes: int = 1,
) -> dict[str, Any]:
    """``start_sweep`` input. Shape captured for a one-room vacuum+mop clean.

    Whole-home cleans use the same object with every room id: a separate
    whole-home payload was not captured. ``passes`` other than 1 were not captured.
    """
    return {
        "sweep_mode": 1,
        "sweep_type": sweep_type,
        "sweep_times": passes,
        "mop_times": passes,
        "suction_level": suction,
        "water_level": water,
        "map_id": map_id,
        "area_ids": list(room_ids),
    }


def decode_map_blob(blob: str) -> bytes:
    """``get_current_map`` returns the grid as gzip compressed bytes, base64 encoded."""
    try:
        return gzip.decompress(base64.b64decode(blob))
    except (OSError, ValueError) as err:
        raise ValueError("current_map is not gzip+base64") from err


def result_code(data: dict[str, Any]) -> int | None:
    """Return the numeric result code from an action ack or a property reply."""
    if "code" in data:
        return _as_int(data.get("code"))
    payload = data.get("payload")
    if isinstance(payload, dict) and "code" in payload:
        return _as_int(payload.get("code"))
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _unwrap(value: Any) -> Any:
    """Property values arrive as ``{"time": ms, "value": ...}``.

    A write we just sent has ``value`` and no ``time`` yet; treat that the same
    way so local state matches the list we stored.
    """
    if isinstance(value, dict) and "value" in value and set(value).issubset({"time", "value"}):
        return value["value"]
    return value


@dataclass
class ThingSchedule:
    """One ``clean.timing`` entry.

    Wire format, fields separated by ``-`` and entries by ``;``::

        id-state-time-days-repeat-map_id-suction-water-sweep_type-sweep_times-mop_times-rooms

    ``state`` 1 is enabled, 0 disabled, 2 shown as invalid in the app.
    ``days`` is 7 characters, index 0 = Sunday.
    ``time`` is ``HH:MM`` for a weekly entry. A one-shot entry (repeat 0,
    days ``0000000``) stores a unix epoch. Printing that epoch in UTC gives
    10:30 on 10/3 for id 8 and 16:45 on 9/21 for id 9. The capture does not
    include the clock the app showed, so that match is not proven.
    ``sweep_type`` / ``sweep_times`` / ``mop_times`` are the three fields the
    capture left unnamed; 0 in the first of them matches a vacuum-only task.
    """

    id: int
    state: int
    time: str
    days: str
    repeat: int
    map_id: int
    suction: int
    water: int
    sweep_type: int
    sweep_times: int
    mop_times: int
    rooms: list[int]

    def encode(self) -> str:
        rooms = ",".join(str(room) for room in self.rooms)
        return (
            f"{self.id}-{self.state}-{self.time}-{self.days}-{self.repeat}-"
            f"{self.map_id}-{self.suction}-{self.water}-{self.sweep_type}-"
            f"{self.sweep_times}-{self.mop_times}-{rooms}"
        )

    @property
    def enabled(self) -> bool:
        return self.state == 1

    @property
    def days_label(self) -> str:
        if self.days == "0000000":
            return "once"
        return ", ".join(name for name, bit in zip(_DAY_NAMES, self.days, strict=True) if bit == "1")

    @property
    def time_label(self) -> str:
        if self.repeat == 0 and self.time.isdigit():
            moment = datetime.fromtimestamp(int(self.time), timezone.utc)
            return f"{moment.hour}:{moment.minute:02d} {moment.month}/{moment.day}"
        return self.time

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "enabled": self.enabled,
            "state": self.state,
            "time": self.time_label,
            "time_raw": self.time,
            "days": self.days_label,
            "days_raw": self.days,
            "repeat": bool(self.repeat),
            "rooms": list(self.rooms),
            "suction": self.suction,
            "water": self.water,
            "sweep_type": self.sweep_type,
            "sweep_times": self.sweep_times,
            "mop_times": self.mop_times,
            "map_id": self.map_id,
        }


def parse_schedule_entry(part: str) -> ThingSchedule:
    fields = part.split("-")
    if len(fields) != 12:
        raise ValueError(f"schedule entry has {len(fields)} fields, expected 12")
    days = fields[3]
    if len(days) != 7 or any(bit not in "01" for bit in days):
        raise ValueError(f"schedule days {days!r} must be 7 bits, Sunday first")
    rooms = [int(room) for room in fields[11].split(",") if room != ""]
    return ThingSchedule(
        id=int(fields[0]),
        state=int(fields[1]),
        time=fields[2],
        days=days,
        repeat=int(fields[4]),
        map_id=int(fields[5]),
        suction=int(fields[6]),
        water=int(fields[7]),
        sweep_type=int(fields[8]),
        sweep_times=int(fields[9]),
        mop_times=int(fields[10]),
        rooms=rooms,
    )


def timing_inner(value: str) -> str:
    """The ``;``-separated list inside ``clean.timing``'s JSON wrapper."""
    text = value.strip()
    if text.startswith("{"):
        loaded = json.loads(text)
        if isinstance(loaded, dict) and "time" in loaded:
            return str(loaded["time"])
    return text


def parse_timing_value(value: str) -> list[ThingSchedule]:
    inner = timing_inner(value)
    if not inner:
        return []
    return [parse_schedule_entry(part) for part in inner.split(";") if part]


def encode_schedules(entries: list[ThingSchedule]) -> str:
    return ";".join(entry.encode() for entry in entries)


def wrap_timing(inner: str) -> str:
    """JSON string the robot stores in ``clean.timing``."""
    return json.dumps({"time": inner}, separators=(",", ":"))


def set_schedule_enabled(entries: list[ThingSchedule], schedule_id: int, enabled: bool) -> str:
    """Return the full timing list with one entry's state flipped.

    The robot replaces the whole list on every write, so the caller sends
    this string back unchanged apart from that one state field.
    """
    found = False
    for entry in entries:
        if entry.id == schedule_id:
            entry.state = 1 if enabled else 0
            found = True
    if not found:
        known = sorted(entry.id for entry in entries)
        raise ValueError(f"No schedule with id {schedule_id}; known ids: {known}")
    return encode_schedules(entries)


def once_timestamp(date: str, clock: str) -> int:
    """Epoch for a one-time entry.

    ``date`` is ``YYYY-MM-DD``. ``clock`` is ``HH:MM`` or ``HH:MM:SS`` (the
    Home Assistant time selector sends seconds). The clock is tagged as UTC,
    which is what makes captured ids 8 and 9 print as 10:30 10/3 and
    16:45 9/21. The capture has no app screenshot of those times.
    """
    text = clock.strip()
    if len(text) == 8 and text[2] == ":" and text[5] == ":":
        text = text[:5]
    parsed = datetime.strptime(f"{date.strip()} {text}", "%Y-%m-%d %H:%M")
    return int(parsed.replace(tzinfo=timezone.utc).timestamp())


def upsert_one_time(
    entries: list[ThingSchedule],
    *,
    when: int,
    rooms: list[int],
    map_id: int,
    suction: int,
    water: int,
    sweep_type: int,
    passes: int,
) -> str:
    """Enable one one-shot entry, reusing a disabled one-shot row when one exists.

    The whole list is returned for a single ``set``. A disabled entry with
    ``repeat`` 0 and days ``0000000`` (the captured "once" rows) is overwritten.
    Otherwise a new id, one higher than the current maximum, is appended.
    """
    reusable = next(
        (entry for entry in entries if entry.repeat == 0 and entry.days == "0000000" and entry.state == 0),
        None,
    )
    if reusable is None:
        new_id = max((entry.id for entry in entries), default=0) + 1
        entries.append(ThingSchedule(
            id=new_id,
            state=1,
            time=str(when),
            days="0000000",
            repeat=0,
            map_id=map_id,
            suction=suction,
            water=water,
            sweep_type=sweep_type,
            sweep_times=passes,
            mop_times=passes,
            rooms=list(rooms),
        ))
    else:
        reusable.state = 1
        reusable.time = str(when)
        reusable.map_id = map_id
        reusable.suction = suction
        reusable.water = water
        reusable.sweep_type = sweep_type
        reusable.sweep_times = passes
        reusable.mop_times = passes
        reusable.rooms = list(rooms)
    return encode_schedules(entries)


def delete_schedule_id(entries: list[ThingSchedule], schedule_id: int) -> str:
    """Drop one entry and return the remaining list.

    The last remaining entry is not deleted here: an empty write would clear
    every schedule, so the caller should disable that entry instead.
    """
    kept = [entry for entry in entries if entry.id != schedule_id]
    if len(kept) == len(entries):
        known = sorted(entry.id for entry in entries)
        raise ValueError(f"No schedule with id {schedule_id}; known ids: {known}")
    if not kept:
        raise ValueError("Refusing to delete the last schedule; disable it instead")
    return encode_schedules(kept)


@dataclass
class ThingRoom:
    room_id: int
    name: str

    @property
    def display_name(self) -> str:
        return self.name.strip() or str(self.room_id)


@dataclass
class ThingState:
    """Latest ThingModel properties, merged from reads and pushes."""

    device_reachable: bool = False
    robot_status: dict[str, Any] = field(default_factory=dict)
    clean: dict[str, Any] = field(default_factory=dict)
    map_props: dict[str, Any] = field(default_factory=dict)
    rooms: list[ThingRoom] = field(default_factory=list)
    map_id: int | None = None
    schedules: list[ThingSchedule] = field(default_factory=list)
    raw_timing: str = ""

    def merge_modules(self, modules: dict[str, Any]) -> None:
        for name, props in modules.items():
            if name == "code" or not isinstance(props, dict):
                continue
            flat = {key: _unwrap(value) for key, value in props.items()}
            if name == "robot_status":
                self.robot_status.update(flat)
            elif name == "clean":
                self.clean.update(flat)
                if "timing" in flat and isinstance(flat["timing"], str):
                    self.raw_timing = timing_inner(flat["timing"])
                    self.schedules = parse_timing_value(self.raw_timing)
            elif name == "map":
                self.map_props.update(flat)
                if "current_room_info" in flat:
                    self._apply_rooms(flat["current_room_info"])
            # Other modules (consumables, custom) are kept only if we asked;
            # they are not required for the vacuum entity.

    def _apply_rooms(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        map_id = _as_int(value.get("mapId", value.get("map_id")))
        if map_id:
            self.map_id = map_id
        raw = value.get("rooms")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return
        if not isinstance(raw, list):
            return
        rooms: list[ThingRoom] = []
        for item in raw:
            if not isinstance(item, dict) or "roomId" not in item:
                continue
            room_id = _as_int(item.get("roomId"))
            if room_id is None:
                continue
            rooms.append(ThingRoom(room_id=room_id, name=str(item.get("roomName") or room_id)))
        if rooms:
            self.rooms = rooms

    @property
    def status_code(self) -> int | None:
        return _as_int(self.robot_status.get("status"))

    @property
    def fault(self) -> int | None:
        return _as_int(self.robot_status.get("fault"))

    @property
    def in_station(self) -> bool | None:
        value = self.robot_status.get("robot_in_station")
        if isinstance(value, bool):
            return value
        return None

    @property
    def station_contacted(self) -> bool | None:
        value = self.robot_status.get("station_contacted")
        if isinstance(value, bool):
            return value
        return None

    @property
    def battery(self) -> int | None:
        return _as_int(self.robot_status.get("battery_percentage"))

    @property
    def suction(self) -> int | None:
        return _as_int(self.clean.get("suction_level"))

    @property
    def water(self) -> int | None:
        return _as_int(self.clean.get("water_level"))

    @property
    def progress(self) -> int | None:
        """Task completion percent.

        A finished job reports 100. A new job starts at 0 and climbs in coarse
        steps (live: 2, 3, 5, 17, 21 while one room was still running). It is
        not a smooth 0–100 gauge, and it is not a room id.
        """
        return _as_int(self.clean.get("cleaning_progress"))

    @property
    def cleaning_time(self) -> int | None:
        """Whole minutes. A ~40 s clean stayed at 0–1; a finished job reported 3."""
        return _as_int(self.clean.get("cleaning_time"))

    @property
    def cleaning_area(self) -> int | None:
        """Whole square meters. The same samples were 0–1 during a short clean and 3 when finished."""
        return _as_int(self.clean.get("cleaning_area"))

    @property
    def clean_room_ids(self) -> list[int]:
        raw = self.clean.get("clean_rooms") or []
        if not isinstance(raw, list):
            return []
        return [room for item in raw if (room := _as_int(item)) is not None]

    @property
    def activity(self) -> str:
        if self.fault:
            return ACTIVITY_ERROR
        code = self.status_code
        if code in STATUS_ACTIVITY:
            return STATUS_ACTIVITY[code]
        if self.in_station:
            return ACTIVITY_DOCKED
        return ACTIVITY_IDLE

    @property
    def status_text(self) -> str:
        if self.fault:
            return f"Fault {self.fault}"
        code = self.status_code
        if code in STATUS_TEXT:
            return STATUS_TEXT[code]
        if code is None:
            return "Unknown"
        return f"Status {code}"

    @property
    def task_active(self) -> bool:
        """True while the robot is cleaning, paused, or driving home."""
        return self.status_code in _ACTIVE_STATUSES

    @property
    def is_charging(self) -> bool | None:
        """Docked and not full. The robot has no separate charging flag."""
        if self.in_station is not True:
            return False if self.in_station is False else None
        if self.battery is None:
            return None
        return self.battery < 100

    def room_name(self, room_id: int) -> str | None:
        """Map room name. None when this id is not in the loaded map."""
        for room in self.rooms:
            if room.room_id == room_id:
                return room.display_name
        return None

    @property
    def current_room(self) -> str | None:
        """Name of the room in the running task.

        ThingModel pushes carry ``clean.clean_rooms`` (the rooms in the task),
        not a robot pose or a separate current-zone field. The name comes from
        the map's room list. An id with no loaded name is omitted, not shown
        as a number.
        """
        if self.activity not in (ACTIVITY_CLEANING, ACTIVITY_PAUSED):
            return None
        names = [name for room_id in self.clean_room_ids if (name := self.room_name(room_id))]
        if not names:
            return None
        return ", ".join(names)


def apply_thing_message(state: ThingState, topic: str, data: dict[str, Any]) -> bool:
    """Merge one MQTT payload into ``state``. True when properties changed."""
    if not isinstance(data, dict):
        return False
    suffix = thing_suffix(topic)
    changed = False
    if suffix == "property/up/reply" and data.get("method") == "getReported":
        payload = data.get("payload")
        if isinstance(payload, dict) and result_code(data) in (0, None):
            state.merge_modules(payload)
            changed = True
    elif suffix == "property/down":
        props = data.get("property")
        if isinstance(props, dict):
            state.merge_modules(props)
            changed = True
    elif suffix == "event/down" and data.get("eventIdentifier") == "map_updated":
        output = data.get("output") or {}
        map_id = _as_int(output.get("map_id") or output.get("mapId"))
        if map_id:
            state.map_id = map_id
            changed = True
    if changed:
        state.device_reachable = True
    return changed
