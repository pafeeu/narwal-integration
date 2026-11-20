"""Constants for the Narwal integration."""

from collections.abc import Mapping
from typing import Any

from homeassistant.const import Platform

from .narwal_client.const import CleanMode, FanLevel, MopHumidity
from .narwal_client.thing_protocol import PROTOCOL_PROTOBUF, PROTOCOL_THING, uses_thing_model

DOMAIN = "narwal"

PLATFORMS = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
]

# Config entry data keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_REGION = "region"
CONF_PRODUCT_KEY = "product_key"
CONF_DEVICE_NAME = "device_name"
CONF_USER_UUID = "user_uuid"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_PROTOCOL = "protocol"
CONF_PRODUCT_GROUP = "product_group"
CONF_MODEL = "model"
CONF_FIRMWARE = "firmware"

# Freo S options. Used when a clean starts without its own parameters.
OPT_CLEAN_MODE = "clean_mode"
OPT_PASSES = "passes"
OPT_SUCTION = "suction"
OPT_WATER = "water"
DEFAULT_CLEAN_MODE = "Vacuum Only"
DEFAULT_PASSES = 1
DEFAULT_SUCTION = "Normal"
DEFAULT_WATER = "medium"

# Legacy keys (kept for migration from manual config)
CONF_MQTT_USERNAME = "mqtt_username"
CONF_MQTT_PASSWORD = "mqtt_password"
CONF_MQTT_CLIENT_ID = "mqtt_client_id"

# Cached room list so start_clean works while the vacuum sleeps.
CONF_ROOMS_CACHE = "rooms_cache"

REGION_OPTIONS = {
    "us": "United States",
    "il": "Israel",
    "eu": "Europe",
    "cn": "China",
}

# Fan speed names shown in HA (order = app order).
FAN_SPEED_MAP: dict[str, FanLevel] = {
    "Quiet": FanLevel.QUIET,
    "Normal": FanLevel.NORMAL,
    "Strong": FanLevel.STRONG,
    "Max": FanLevel.MAX,
}
FAN_SPEED_LIST = list(FAN_SPEED_MAP)
FAN_SPEED_REVERSE = {v: k for k, v in FAN_SPEED_MAP.items()}

# Clean mode names are also used by the Lovelace card and send_command.
CLEAN_MODE_MAP: dict[str, CleanMode] = {
    "Vacuum & Mop": CleanMode.VACUUM_AND_MOP,
    "Vacuum then Mop": CleanMode.VACUUM_THEN_MOP,
    "Vacuum Only": CleanMode.VACUUM_ONLY,
    "Mop Only": CleanMode.MOP_ONLY,
}
CLEAN_MODE_LIST = list(CLEAN_MODE_MAP)
CLEAN_MODE_REVERSE = {v: k for k, v in CLEAN_MODE_MAP.items()}

MOP_HUMIDITY_MAP: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}


def entry_protocol(data: Mapping[str, Any]) -> str:
    """Protocol for a config entry.

    An explicit ``protocol`` value wins. Entries created before Freo S support
    have none, so the product key and product group decide. Freo X Ultra stays
    on protobuf; Freo S (BX3 / ``ZTM5lnVgLG``) uses ThingModel.
    """
    stored = data.get(CONF_PROTOCOL)
    if stored in (PROTOCOL_PROTOBUF, PROTOCOL_THING):
        return str(stored)
    return PROTOCOL_THING if uses_thing_model(
        product_key=str(data.get(CONF_PRODUCT_KEY) or ""),
        product_group=str(data.get(CONF_PRODUCT_GROUP) or ""),
    ) else PROTOCOL_PROTOBUF
