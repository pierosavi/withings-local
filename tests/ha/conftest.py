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
FD77_UUID = "0000fd77-0000-1000-8000-00805f9b34fb"
# Body Comp (model 0x12) advertises only FD77 data: flags 03 (installed, sync requested) after a
# weigh-in in Bluetooth mode; flags 00 plus its MAC when not set up. The trailing bytes are synthetic.
BODY_COMP_INSTALLED_FD77 = bytes.fromhex("1012 11 03 34 01020304")
BODY_COMP_IDLE_FD77 = bytes.fromhex("1012 11 01 34 01020304")
BODY_COMP_SETUP_FD77 = bytes.fromhex("1012 11 00 26 001122334455")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def service_info(
    name="Body Smart 55", uuid=INSTALLED_UUID, address=ADDRESS, mfr=b"\x01\x02\x03\x04", service_data=None
) -> BluetoothServiceInfoBleak:
    service_data = service_data or {}
    uuids = [uuid] if uuid else []
    adv = AdvertisementData(
        local_name=name,
        manufacturer_data={0x03FF: mfr} if mfr is not None else {},
        service_data=service_data,
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
        service_data=service_data,
        service_uuids=uuids,
        source="local",
        device=BLEDevice(address, name, {}),
        advertisement=adv,
        connectable=True,
        time=time.monotonic(),
        tx_power=0,
    )


def body_comp_info(fd77=BODY_COMP_INSTALLED_FD77, name="Body Comp 55") -> BluetoothServiceInfoBleak:
    return service_info(name=name, uuid=None, mfr=None, service_data={FD77_UUID: fd77})
