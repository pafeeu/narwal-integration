#!/usr/bin/env python3
"""Live checks for a Narwal Freo S (product group BX3, ThingModel JSON).

Needs paho-mqtt only (``pip install -r requirements.txt``). It does not
import Home Assistant. Port 8883 to the Narwal broker is often blocked off
the home server, so run it inside the Home Assistant container, where
``python3`` and paho are already available:

  docker exec -it <ha-container> python3 /config/scripts/freo_s_live.py

Copy or mount this repo where that path points, and export the variables
below in that environment (or use scripts/.env next to this file). Values
already set in the environment win. Nothing here is printed back except
robot state.

  NARWAL_EMAIL       account email
  NARWAL_PASSWORD    account password
  NARWAL_REGION      us (default), il, eu, or cn
  NARWAL_DEVICE_ID   optional 32-hex device id, required if several
                     ThingModel robots are on the account

Read-only (default, and each of these on its own):

  python scripts/freo_s_live.py
  python scripts/freo_s_live.py status
  python scripts/freo_s_live.py map
  python scripts/freo_s_live.py schedules
  python scripts/freo_s_live.py heartbeat
  python scripts/freo_s_live.py map-grid

Commands that move the robot or rewrite schedules. Each one is separate
and refuses to run without --yes:

  python scripts/freo_s_live.py recall --yes
  python scripts/freo_s_live.py pause --yes
  python scripts/freo_s_live.py resume --yes
  python scripts/freo_s_live.py stop --yes
  python scripts/freo_s_live.py start --rooms 5 --yes
  python scripts/freo_s_live.py set-schedule --id 10 --enabled false --yes
  python scripts/freo_s_live.py set-schedule --timing '<full list>' --yes
  python scripts/freo_s_live.py set-suction 1 --yes
  python scripts/freo_s_live.py set-water 2 --yes
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Load narwal_client by itself. Importing custom_components.narwal runs the
# integration's __init__, which needs Home Assistant. This script only needs
# paho-mqtt, so it also runs in the HA container or a plain Python checkout.
sys.path.insert(0, str(ROOT / "custom_components" / "narwal"))

from narwal_client.cloud import NarwalCloud, NarwalCloudError  # noqa: E402
from narwal_client.thing_client import ThingClient  # noqa: E402
from narwal_client.thing_protocol import (  # noqa: E402
    encode_schedules,
    model_name,
    parse_timing_value,
    set_schedule_enabled,
    uses_thing_model,
)

_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


def _safe(text: str) -> str:
    return _JWT.sub("<JWT>", text)


def _load_env_file() -> None:
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _redact(device_id: str) -> str:
    if len(device_id) < 8:
        return "****"
    return f"{device_id[:4]}…{device_id[-4:]}"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: set {name}.", file=sys.stderr)
        raise SystemExit(2)
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Live Freo S ThingModel checks. Default is read-only.")
    sub = parser.add_subparsers(dest="command")
    for name in ("reads", "status", "map", "schedules", "heartbeat", "map-grid"):
        sub.add_parser(name)

    def mutating(name: str, **kwargs) -> argparse.ArgumentParser:
        command = sub.add_parser(name, **kwargs)
        command.add_argument("--yes", action="store_true", help="Required. This command changes the robot.")
        return command

    for name in ("recall", "pause", "resume", "stop"):
        mutating(name)
    start = mutating("start", help="start_sweep for the given room ids")
    start.add_argument("--rooms", required=True, help="Comma-separated room ids, for example 5 or 6,8")
    start.add_argument("--suction", type=int, default=2)
    start.add_argument("--water", type=int, default=2)
    start.add_argument("--sweep-type", type=int, default=2, dest="sweep_type")
    start.add_argument("--passes", type=int, default=1)
    sched = mutating("set-schedule", help="Rewrite the whole schedule list")
    sched.add_argument("--id", type=int, dest="schedule_id")
    sched.add_argument("--enabled", choices=("true", "false"))
    sched.add_argument("--timing", help="Full ;-separated timing list")
    suction = mutating("set-suction", help="Write clean.suction_level (1 Quiet … 4 Max)")
    suction.add_argument("level", type=int, choices=(1, 2, 3, 4))
    water = mutating("set-water", help="Write clean.water_level (1–3). Unverified live.")
    water.add_argument("level", type=int, choices=(1, 2, 3))
    return parser


def _print_state(client: ThingClient) -> None:
    state = client.state
    print(f"activity: {state.activity} ({state.status_text}, code {state.status_code})")
    print(f"battery: {state.battery}%  docked: {state.in_station}  charging: {state.is_charging}  fault: {state.fault}")
    print(f"suction: {state.suction}  water: {state.water}  progress_percent: {state.progress}")
    print(f"cleaning_time_min: {state.cleaning_time}  cleaning_area_m2: {state.cleaning_area}")
    print(f"clean_rooms: {state.clean_room_ids}  current_room: {state.current_room or '-'}")
    print(f"map_id: {state.map_id}")
    if state.rooms:
        print("rooms: " + ", ".join(f"{room.room_id} {room.display_name}" for room in state.rooms))
    else:
        print("rooms: (none reported)")


def _print_schedules(client: ThingClient) -> None:
    entries = client.state.schedules
    print(f"schedules: {len(entries)} ({sum(entry.enabled for entry in entries)} enabled)")
    for entry in entries:
        flag = "on" if entry.enabled else "off"
        print(
            f"  {entry.id} {flag} {entry.time_label()} {entry.days_label} "
            f"rooms={entry.rooms} suction={entry.suction} water={entry.water}"
        )


async def _reads(client: ThingClient, which: str) -> None:
    if which in ("reads", "status"):
        await client.get_reported({"robot_status": []})
        await client.get_reported({"clean": []})
        _print_state(client)
    if which in ("reads", "map"):
        await client.get_reported({"map": []})
        _print_state(client)
    if which in ("reads", "schedules"):
        await client.read_timing()
        _print_schedules(client)
    if which in ("reads", "heartbeat"):
        await client.heartbeat()
        print("heartbeat: ok")
    if which == "map-grid":
        blob = await client.get_current_map()
        print(f"map-grid: {len(blob)} bytes (contents not printed)")


async def _mutate(client: ThingClient, args: argparse.Namespace) -> None:
    command = args.command
    if command == "recall":
        await client.recall()
        print("recall_station: ok")
    elif command == "pause":
        await client.pause()
        print("pause_sweeping: ok")
    elif command == "resume":
        await client.resume()
        print("continue_sweep: ok")
    elif command == "stop":
        await client.stop()
        print("stop_sweeping: ok")
    elif command == "start":
        await client.get_reported({"map": []})
        rooms = [int(part) for part in args.rooms.split(",") if part.strip()]
        map_id = client.state.map_id
        if not map_id:
            raise SystemExit("The robot has not reported a map id.")
        await client.start_sweep(
            rooms,
            map_id=map_id,
            suction=args.suction,
            water=args.water,
            sweep_type=args.sweep_type,
            passes=args.passes,
        )
        print(f"start_sweep: ok rooms={rooms} map_id={map_id}")
    elif command == "set-schedule":
        if args.timing:
            inner = args.timing.strip()
            if encode_schedules(parse_timing_value(inner)) != inner:
                raise SystemExit("timing does not match the Freo S schedule format; nothing was written.")
            await client.write_timing(inner)
            print("set timing: ok")
        elif args.schedule_id is not None:
            if args.enabled is None:
                raise SystemExit("set-schedule --id needs --enabled true or false.")
            current = await client.read_timing()
            updated = set_schedule_enabled(
                parse_timing_value(current), args.schedule_id, args.enabled == "true"
            )
            await client.write_timing(updated)
            print(f"schedule {args.schedule_id} enabled={args.enabled}: ok")
        else:
            raise SystemExit("set-schedule needs --timing, or --id and --enabled.")
    elif command == "set-suction":
        await client.set_suction(args.level)
        print(f"set suction_level={args.level}: ok")
    elif command == "set-water":
        await client.set_water(args.level)
        print(f"set water_level={args.level}: ok (unverified live path)")
    await client.get_reported({"robot_status": []})
    await client.get_reported({"clean": []})
    _print_state(client)


async def _main(args: argparse.Namespace) -> None:
    command = args.command or "reads"
    mutating = command in {
        "recall", "pause", "resume", "stop", "start", "set-schedule", "set-suction", "set-water",
    }
    if mutating and not args.yes:
        print(f"ERROR: {command} changes the robot. Re-run with --yes if you mean it.", file=sys.stderr)
        raise SystemExit(2)

    _load_env_file()
    email = _require("NARWAL_EMAIL")
    password = _require("NARWAL_PASSWORD")
    region = os.environ.get("NARWAL_REGION", "us").strip() or "us"
    wanted = os.environ.get("NARWAL_DEVICE_ID", "").strip().lower()

    cloud = NarwalCloud(region=region)
    try:
        await asyncio.to_thread(cloud.login, email, password)
        devices = await asyncio.to_thread(cloud.get_devices)
    except NarwalCloudError as err:
        print(f"ERROR: cloud login failed: {_safe(str(err))}", file=sys.stderr)
        raise SystemExit(1) from err

    thing = [
        device for device in devices
        if uses_thing_model(
            product_key=device.product_key,
            product_group=device.product_group,
            thing_flag=device.thing_model,
        )
    ]
    if wanted:
        thing = [device for device in thing if device.device_name.lower() == wanted]
    if not thing:
        print("ERROR: no ThingModel robot matched this account.", file=sys.stderr)
        raise SystemExit(1)
    if len(thing) > 1:
        print("ERROR: several ThingModel robots. Set NARWAL_DEVICE_ID to one of:", file=sys.stderr)
        for device in thing:
            print(f"  {_redact(device.device_name)} {device.product_key}", file=sys.stderr)
        raise SystemExit(1)

    device = thing[0]
    print(
        f"robot: {model_name(device.product_key, device.product_group, device.model) or device.model} "
        f"product {device.product_key} group {device.product_group or '-'} device {_redact(device.device_name)}"
    )
    client = ThingClient(
        product_key=device.product_key,
        device_name=device.device_name,
        user_uuid=cloud.session.user_uuid,
        mqtt_password=cloud.session.access_token,
        broker=cloud.mqtt_broker,
    )
    await client.connect()
    try:
        if mutating:
            await _mutate(client, args)
        else:
            await _reads(client, command)
    finally:
        await client.disconnect()


def main() -> None:
    args = _parser().parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as err:
        print(f"ERROR: {_safe(str(err))}", file=sys.stderr)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
