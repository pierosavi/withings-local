"""Fixtures for the Home Assistant integration tests. All values are synthetic."""

import time

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

ADDRESS = "00:11:22:33:44:55"
INSTALLED_UUID = "00000020-5749-5448-0010-001122334455"  # tail = MAC: installed, has data
SETUP_UUID = "00000020-5749-5448-0010-000000000000"  # tail zeros: button/setup session
WBS06_ADDRESS = "02:00:00:00:56:5F"  # synthetic locally administered address
WBS06_IDENTITY = "02:00:00:00:56:5E"
WBS06_UUID = "00000020-5749-5448-0007-02000000565e"
WBS06_SETUP_UUID = "00000020-5749-5448-0007-000000000000"
TEST_KL = "test-kl-not-a-real-secret-000000"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def service_info(name="Body Smart 55", uuid=INSTALLED_UUID, address=ADDRESS, mfr=b"\x01\x02\x03\x04") -> BluetoothServiceInfoBleak:
    uuids = [uuid] if uuid else []
    adv = AdvertisementData(
        local_name=name,
        manufacturer_data={0x03FF: mfr} if mfr is not None else {},
        service_data={},
        service_uuids=uuids,
        tx_power=0,
        rssi=-60,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data=adv.manufacturer_data,
        service_data={},
        service_uuids=uuids,
        source="local",
        device=BLEDevice(address, name, {}),
        advertisement=adv,
        connectable=True,
        time=time.monotonic(),
        tx_power=0,
    )
