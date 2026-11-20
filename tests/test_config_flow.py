"""Config flow: robots are discovered from the account after login."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.const import DOMAIN
from custom_components.narwal.narwal_client import NarwalAuthError, NarwalCloudError
from custom_components.narwal.narwal_client.cloud import NarwalCloud, NarwalDevice

LOGIN = {"email": "user@example.com", "password": "secret", "region": "il"}
ROBOT_A = NarwalDevice("a" * 32, "EHf6cRNRGT", "Freo", "Narwal Freo X Ultra", "v01.09.01.00")
ROBOT_B = NarwalDevice("b" * 32, "EHf6cRNRGT", "Upstairs", "Narwal Freo X Ultra")

# A real-shaped getDeviceInfoList response (values replaced).
DEVICE_LIST_RESPONSE = {
    "code": 0,
    "msg": "ok",
    "result": {
        "deviceInfoList": [{
            "deviceId": "a" * 32,
            "productId": "EHf6cRNRGT",
            "robotName": "Freo",
            "firmwareVersion": "v01.09.01.00",
            "snNumber": "SN",
        }],
        "productInfoList": [{"productId": "EHf6cRNRGT", "productModel": "Narwal Freo X Ultra", "productGroup": "J4"}],
    },
}


def _fake_login(self: NarwalCloud, email: str, password: str):
    self.session.access_token = "access"
    self.session.refresh_token = "refresh"
    self.session.user_uuid = "0" * 32
    return self.session


@pytest.fixture(autouse=True)
def no_setup():
    """Don't actually set up the entry after the flow creates it."""
    with patch("custom_components.narwal.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant, devices=None, error=None):
    def get_devices(self):
        if error:
            raise error
        return devices

    with (
        patch.object(NarwalCloud, "login", _fake_login),
        patch.object(NarwalCloud, "get_devices", get_devices),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
        return await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)


def test_device_list_parsing() -> None:
    cloud = NarwalCloud(region="il")
    with patch.object(NarwalCloud, "_request", return_value=DEVICE_LIST_RESPONSE) as req:
        devices = cloud.get_devices()
    assert req.call_args.args[:2] == ("GET", "/user-device-platform-server/device-info/getDeviceInfoList")
    assert devices == [NarwalDevice("a" * 32, "EHf6cRNRGT", "Freo", "Narwal Freo X Ultra", "v01.09.01.00")]
    assert devices[0].label == "Freo (Narwal Freo X Ultra)"
    assert devices[0].product_group == "J4"
    assert devices[0].thing_model is None


async def test_single_robot_is_added_without_questions(hass: HomeAssistant) -> None:
    result = await _start(hass, devices=[ROBOT_A])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Freo"
    assert result["data"]["device_name"] == "a" * 32
    assert result["data"]["product_key"] == "EHf6cRNRGT"
    assert result["data"]["access_token"] == "access"
    assert result["result"].unique_id == "EHf6cRNRGT_" + "a" * 32


async def test_several_robots_pick_one(hass: HomeAssistant) -> None:
    result = await _start(hass, devices=[ROBOT_A, ROBOT_B])
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pick"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"device_name": "b" * 32})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Upstairs"
    assert result["data"]["device_name"] == "b" * 32


async def test_already_added_robots_are_skipped(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="EHf6cRNRGT_" + "a" * 32).add_to_hass(hass)
    result = await _start(hass, devices=[ROBOT_A, ROBOT_B])
    # Only robot B is left, so it is added directly.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["device_name"] == "b" * 32

    result = await _start(hass, devices=[ROBOT_A, ROBOT_B])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_no_robots_on_account(hass: HomeAssistant) -> None:
    result = await _start(hass, devices=[])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_devices"


async def test_device_list_failure_falls_back_to_manual(hass: HomeAssistant) -> None:
    result = await _start(hass, error=NarwalCloudError("boom"))
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"

    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"product_key": "EHf6cRNRGT", "device_name": "not-hex"}
    )
    assert bad["errors"] == {"device_name": "invalid_device_name"}

    done = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"product_key": "EHf6cRNRGT", "device_name": "C" * 32}
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["data"]["device_name"] == "c" * 32


async def test_wrong_password(hass: HomeAssistant) -> None:
    def bad_login(self, email, password):
        raise NarwalAuthError("Account or password is incorrect")

    with patch.object(NarwalCloud, "login", bad_login):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "invalid_auth"}


async def test_freo_s_is_marked_thing_model_even_when_the_flag_is_false(hass: HomeAssistant) -> None:
    """BX3 speaks ThingModel whether or not switch_to_robothings is set."""
    robot = NarwalDevice("c" * 32, "ZTM5lnVgLG", "Downstairs", "", "v01.01.64.00", "BX3", False)
    result = await _start(hass, devices=[robot])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["protocol"] == "thing"
    assert result["data"]["product_group"] == "BX3"
    assert result["data"]["product_key"] == "ZTM5lnVgLG"
    assert result["data"]["model"] == "Narwal Freo S"
    assert result["data"]["firmware"] == "v01.01.64.00"


async def test_switch_to_robothings_selects_thing_model_for_an_unknown_product(hass: HomeAssistant) -> None:
    robot = NarwalDevice("d" * 32, "NEWPRODUCT", "New", "Future model", thing_model=True)
    result = await _start(hass, devices=[robot])
    assert result["data"]["protocol"] == "thing"
    assert result["data"]["model"] == "Future model"


async def test_manual_freo_s_product_key_selects_thing_model(hass: HomeAssistant) -> None:
    result = await _start(hass, error=NarwalCloudError("boom"))
    done = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"product_key": "ZTM5lnVgLG", "device_name": "e" * 32}
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["data"]["protocol"] == "thing"
    assert done["data"]["model"] == "Narwal Freo S"


def test_device_list_reads_the_thing_model_flag() -> None:
    cloud = NarwalCloud(region="us")
    reply = {
        "code": 0,
        "result": {
            "deviceInfoList": [{
                "deviceId": "f" * 32,
                "productId": "NEWPRODUCT",
                "robotName": "New",
                "application_user_params": {"switch_to_robothings": True},
            }],
            "productInfoList": [{
                "productId": "NEWPRODUCT",
                "productModel": "Future model",
                "productGroup": "ZZ",
            }],
        },
    }
    with patch.object(NarwalCloud, "_request", return_value=reply):
        device = cloud.get_devices()[0]
    assert device.thing_model is True
    assert device.product_group == "ZZ"
    assert device.model == "Future model"


def test_wrong_password_reply_is_an_auth_error() -> None:
    """Narwal answers bad credentials with code -1 (seen live)."""
    cloud = NarwalCloud(region="il")
    reply = {"code": -1, "msg": "Account or password is incorrect, please check and try again"}
    with patch.object(NarwalCloud, "_request", return_value=reply), pytest.raises(NarwalAuthError):
        cloud.login("user@example.com", "wrong")
