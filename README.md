# Narwal Robot Vacuum Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A custom Home Assistant integration for **Narwal** robot vacuums (Freo X Ultra and similar models) that communicates through the Narwal cloud MQTT service.

> **⚠️ Work in Progress** — This integration is under active development. Expect breaking changes, incomplete features, and rough edges. Contributions and bug reports are welcome!

> [!WARNING]
> **There are two Narwal integrations, and they can't be installed together.**
> This one and [sjmotew/NarwalIntegration](https://github.com/sjmotew/NarwalIntegration) both install to `custom_components/narwal` (domain `narwal`), so Home Assistant and HACS can only hold one of them at a time. Pick the one that matches your robot:
>
> - **This integration** talks to the robot through the Narwal cloud (your Narwal app account). It's built and tested on the **Freo X Ultra**, which has no local API.
> - **[sjmotew/NarwalIntegration](https://github.com/sjmotew/NarwalIntegration)** is fully local (WebSocket on port 9002, no account) and targets models that expose that interface, such as the Flow, Flow 2, Freo Z10 / Z10 Ultra, Freo X10 Pro and Freo 20. See its compatibility table.
>
> To switch, remove the installed one first (its integration entry, then the HACS download), restart Home Assistant, and then install the other. Renaming either domain isn't planned: it would break every existing user's config entries and entity IDs ([#9](https://github.com/nadavbau/narwal-integration/issues/9)).

## Features

- **Vacuum entity**: start, stop, pause/resume, return to dock, locate, suction level, live state (cleaning, paused, returning, docked, error)
- **Room cleaning**: native Home Assistant *clean area* support (map rooms to HA areas, then `vacuum.clean_area`), plus `vacuum.send_command` → `clean_rooms` with per-call mode, suction, mop wetness and passes. Rooms marked no-go in the Narwal app are skipped automatically.
- **Live map** camera: rooms, names, dock, robot position and heading, current room highlighted, oriented like the app
- **Dock actions**: dry mops, wash and dry mops, empty dust bin (buttons and `send_command`)
- **Settings**: volume, do-not-disturb, child lock, auto-empty, quiet auto-empty, auto detergent, carpet boost, pet mode, mop drying, mop-wash frequency, carpet handling
- **Status sensors**: battery, detailed status, errors (with the app's own messages), cleaning time, time remaining, cleaned area, progress, current room, clean/dirty water tanks, dust bin, detergent, maintenance items due, charging, docked
- **Cloud login** with email/password and automatic token refresh

## Supported Devices

This integration has been tested with:

- Narwal Freo X Ultra (protobuf cloud protocol)
- Narwal Freo S (ThingModel JSON protocol, product group BX3)

Other Narwal models that use the Freo X Ultra protobuf topics should also work. The integration picks the protocol from the robot's product key, product group, or the cloud flag `switch_to_robothings`. A Freo X Ultra entry is unchanged.

## Freo S (BX3)

The Freo S (product key `ZTM5lnVgLG`, product group `BX3`) answers plain JSON on `/<productKey>/<deviceId>/thing/...` and does not answer the protobuf commands used by the Freo X Ultra. Setup is the same account login. With one robot it is added immediately; the protocol follows the product.

What you get:

- Vacuum: start, pause, resume, stop, return to dock, suction (Quiet / Normal / Strong / Max)
- Room cleaning: Home Assistant 2026.3+ `vacuum.clean_area` (rooms are segments), and `vacuum.send_command` → `clean_rooms`
- Sensors: battery, status, error, docked, charging, cleaning time, cleaned area, progress, current room, schedules
- Water level and clean mode (Vacuum & Mop, or Vacuum Only). Both are sent with the next clean
- Schedules: read on the Schedules sensor; write with `narwal.set_schedules` (always the whole list)

Suction and water are included in `start_sweep`. A separate property write for them was not captured, so changing them while the robot is docked is stored until the next clean. Resume (`continue_sweep`) and stop (`stop_sweeping`) use the same empty invoke envelope as pause; those two action names come from the app and were not in the capture. Whole-home start sends every room id with the same payload as a room clean.

`narwal.set_schedules` either replaces the list or flips one entry after reading the current list:

```yaml
action: narwal.set_schedules
data:
  entity_id: vacuum.narwal_freo_s_narwal_vacuum
  schedule_id: 10
  enabled: false
```

`timing` (the sensor's `timing` attribute) replaces the whole list instead. The string is `id-state-time-days-repeat-map-suction-water-sweep_type-sweep_times-mop_times-rooms`, entries separated by `;`. Days are 7 characters, Sunday first. State `1` is enabled, `0` disabled.

There is no live map image for the Freo S. Room names come from the map property. The Lovelace card still lists those rooms; it hides locate and dock maintenance actions this model does not expose, and it offers only the clean modes the select entity has.

### Live check script

`scripts/freo_s_live.py` talks to a real robot with the same client. Credentials stay in the environment (or `scripts/.env`, which is gitignored). It never prints the password or the token.

Read-only (status, rooms, schedules, one heartbeat):

```bash
pip install -r requirements.txt
export NARWAL_EMAIL='you@example.com'
export NARWAL_PASSWORD='your-password'
export NARWAL_REGION=us   # us, il, eu, or cn. Default for this script: us
# optional, required when the account has more than one ThingModel robot:
# export NARWAL_DEVICE_ID='the 32-character device id'
python scripts/freo_s_live.py
```

One read at a time: `status`, `map`, `schedules`, `heartbeat`, `map-grid` (downloads the map and prints only its size).

Commands that move the robot or write schedules are separate and require `--yes`:

```bash
python scripts/freo_s_live.py recall --yes
python scripts/freo_s_live.py pause --yes
python scripts/freo_s_live.py resume --yes
python scripts/freo_s_live.py stop --yes
python scripts/freo_s_live.py start --rooms 5 --yes
python scripts/freo_s_live.py set-schedule --id 10 --enabled false --yes
```

`start` and `set-schedule` change what the robot does. `recall` drives it home. Run them yourself, one at a time.

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Click the three dots in the top-right corner and select **Custom repositories**
3. Add this repository URL and select **Integration** as the category
4. Search for "Narwal" and install it
5. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/narwal` folder into your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for **Narwal**
3. Sign in with your Narwal app account:
   - **Email** and **Password** — the ones you use in the Narwal app
   - **Region** — where the account is registered (US, Israel, Europe or China)
4. That's it. Your robots are fetched from the Narwal cloud: with one robot it is added straight away; with several you pick one (add the integration again for each robot).

If the robot list can't be loaded, the flow falls back to asking for the product key (`EHf6cRNRGT` for the Freo X Ultra) and the robot's 32-character MQTT device name; `scripts/README.md` explains how to find it.

## Lovelace card

The integration registers a `custom:narwal-vacuum-card` automatically:

```yaml
type: custom:narwal-vacuum-card
entity: vacuum.narwal_vacuum
```

It shows status, battery, error and maintenance banners, the live map, room selection (no-go rooms are disabled), cleaning mode, suction and mop wetness, start/pause/resume, stop, dock, locate, and dock actions (dry mops, wash and dry, empty dust bin). Related entities are found automatically from the vacuum's device; `camera_entity`, `battery_entity` and `mode_entity` can override them.

## Entities

### Vacuum

| Feature | Notes |
|---------|-------|
| Start | Resumes if paused, otherwise cleans all rooms (except no-go rooms) using the **Clean mode** select and **Clean passes** number |
| Stop / Pause / Return to base / Locate | |
| Fan speed | Quiet, Normal, Strong, Max. The robot only accepts changes during a clean; while docked the choice is remembered and used for the next clean |
| Clean area | Home Assistant 2026.3+: open the vacuum's settings → *Map rooms to areas*, then call `vacuum.clean_area` |

`vacuum.send_command` commands:

```yaml
# Clean specific rooms (room ids are in the vacuum's `rooms` attribute)
action: vacuum.send_command
target:
  entity_id: vacuum.narwal_vacuum
data:
  command: clean_rooms
  params:
    rooms: [4, 2]
    mode: "Vacuum & Mop"      # optional: Vacuum & Mop, Vacuum then Mop, Vacuum Only, Mop Only
    fan_speed: Strong         # optional: Quiet, Normal, Strong, Max
    mop_humidity: wet         # optional: dry, normal, wet
    passes: 2                 # optional: 1-3
```

Also: `dry_mop`, `wash_and_dry_mop`, `wash_mop` (mid-clean only), `empty_dustbin`.

### Other entities

| Type | Entities |
|------|----------|
| Sensor | Battery, Status, Error, Cleaning time, Time remaining, Cleaned area, Cleaning progress, Current room, Maintenance items due, Clean water tank, Dirty water tank, Dust bin, Detergent |
| Binary sensor | Charging, Docked, Problem, Maintenance due |
| Select | Clean mode, Mop wetness, Mop drying, Mop washing frequency, Carpet handling |
| Switch | Do not disturb, Child lock, Auto-empty, Quiet auto-empty, Auto detergent, Carpet boost, Pet mode, Smart deep clean*, Hot water mop wash* |
| Number | Volume, Clean passes |
| Button | Locate, Wake up, Dry mops, Wash and dry mops, Wash mops, Empty dust bin |
| Camera | Map |

\* disabled by default; not every model supports them.

## Architecture

This integration communicates with the vacuum via the **Narwal cloud MQTT broker** (MQTT 5 over TLS on port 8883). It does not require local network access to the vacuum.

```
Home Assistant  ──MQTT5/TLS──▶  Narwal Cloud  ──────▶  Vacuum
                                (MQTT Broker)
```

The integration authenticates using the same API as the official Narwal mobile app:

1. **REST API login** (`loginByEmail`) to obtain a JWT access token
2. **MQTT connection** using the JWT as the password
3. **Protobuf-encoded** commands and status messages over MQTT topics. Field numbers and enums come from the official Narwal app's message definitions (`narwal_client/schema.py`), encoded/decoded by `narwal_client/proto.py`
4. **Automatic token refresh** when the JWT approaches expiry

## Development

### Requirements

- Python 3.12+
- Home Assistant 2025.3.0+ (room cleaning by area needs 2026.3+)
- `paho-mqtt` >= 2.0.0

### Tests

The tests run the real integration inside Home Assistant against a fake robot that answers with protocol-correct messages:

```bash
pip install -r requirements_test.txt
pytest
```

### Project Structure

```
custom_components/narwal/
├── __init__.py              # Integration setup/teardown, card registration
├── config_flow.py           # Two-step UI config flow (login + device)
├── const.py                 # HA-level constants (fan speeds, clean modes)
├── coordinator.py           # MQTT pushes + polling, token refresh, map/settings refresh
├── entity.py                # Base entity: device info, command error handling
├── vacuum.py                # Vacuum entity, clean area (segments), send_command
├── sensor.py / binary_sensor.py / select.py / switch.py / number.py / button.py
├── camera.py                # Map rendering (coordinator owns map fetching)
├── strings.json, translations/en.json
├── frontend/narwal-vacuum-card.js
└── narwal_client/           # Standalone Narwal protocol library
    ├── client.py             # MQTT client: connect, commands, pushes
    ├── cloud.py              # REST API client (login, refresh)
    ├── const.py              # Topics, enums matching the app
    ├── models.py             # NarwalState: activity, status, rooms, map geometry
    ├── proto.py              # Schema-driven protobuf encode/decode
    ├── schema.py             # Generated: field numbers/enums from the Narwal app
    ├── error_codes.py        # Robot error code -> app message (J4 platform)
    └── map_renderer.py       # Grid -> PNG with rooms, dock and robot
```

## License

[MIT](LICENSE)
