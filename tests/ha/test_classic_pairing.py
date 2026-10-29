"""Native WBS06 BR/EDR pairing uses only the scoped BlueZ management path."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dbus_fast import MessageType, Variant
from dbus_fast.errors import DBusError
import pytest

from custom_components.withings_local.classic_pairing import (
    ADAPTER,
    CLASSIC,
    DEVICE,
    MGMT_CANCEL_PAIR,
    MGMT_PAIR,
    ClassicAgent,
    ClassicPairingSession,
    _management_packet,
    _pairing_status,
)
from custom_components.withings_local.pairing import PairingFailed

PATH = "/org/bluez/hci0/dev_00_11_22_33_44_55"  # synthetic
FOREIGN = "/org/bluez/hci0/dev_66_77_88_99_AA_BB"
CONTROLLER = "/org/bluez/hci0"


class FakeBus:
    def __init__(self, *, bonded=False, trusted=False, pairable=False):
        self.state = {
            (PATH, CLASSIC, "Paired"): bonded,
            (PATH, CLASSIC, "Bonded"): bonded,
            (PATH, DEVICE, "Trusted"): trusted,
            (CONTROLLER, ADAPTER, "Discoverable"): False,
            (CONTROLLER, ADAPTER, "Pairable"): pairable,
            (CONTROLLER, ADAPTER, "PairableTimeout"): 0,
        }
        self.calls = []
        self.exports = []
        self.unexports = []
        self.closed = False

    async def connect(self):
        return self

    def export(self, path, obj):
        self.exports.append(path)

    def unexport(self, path, obj):
        self.unexports.append(path)

    def disconnect(self):
        self.closed = True

    async def call(self, message):
        self.calls.append((message.interface, message.member))
        if message.member == "Get":
            key = (message.path, message.body[0], message.body[1])
            value = self.state[key]
            return SimpleNamespace(
                message_type=MessageType.METHOD_RETURN,
                body=[Variant("b" if isinstance(value, bool) else "u", value)],
            )
        if message.member == "Set":
            self.state[(message.path, message.body[0], message.body[1])] = message.body[2].value
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[])


def test_pair_command_is_bredr_only_with_no_input_output() -> None:
    # BlueZ mgmt spec: 0x0019, hci0, 8 octets: reversed MAC, type 0, IO 3.
    assert _management_packet(PATH, MGMT_PAIR) == (
        0, bytes.fromhex("19 00 00 00 08 00 55 44 33 22 11 00 00 03")
    )
    assert _management_packet(PATH, MGMT_CANCEL_PAIR) == (
        0, bytes.fromhex("1a 00 00 00 07 00 55 44 33 22 11 00 00")
    )
    with pytest.raises(ValueError):
        _management_packet(PATH, 0x001B)  # Unpair Device is prohibited.


def test_management_result_ignores_other_devices_and_operations() -> None:
    target = bytes.fromhex("55 44 33 22 11 00 00")
    assert _pairing_status(bytes.fromhex("01 00 00 00 0a 00 19 00 00 55 44 33 22 11 00 00"), 0, target) == (True, 0)
    assert _pairing_status(bytes.fromhex("02 00 00 00 03 00 19 00 00"), 0, target) == (False, 0)
    assert _pairing_status(bytes.fromhex("01 00 00 00 0a 00 19 00 04 55 44 33 22 11 00 00"), 0, target) == (True, 4)
    assert _pairing_status(bytes.fromhex("01 00 01 00 0a 00 19 00 00 55 44 33 22 11 00 00"), 0, target) is None
    assert _pairing_status(bytes.fromhex("01 00 00 00 0a 00 1b 00 00 55 44 33 22 11 00 00"), 0, target) is None
    assert _pairing_status(bytes.fromhex("01 00 00 00 03 00 19 00 00"), 0, target) is None


def test_management_result_ignores_a_different_device_on_the_same_adapter() -> None:
    # Command Complete returns seven bytes of paired-device identity.
    unrelated = bytes.fromhex("01 00 00 00 0a 00 19 00 00 66 77 88 99 aa bb 00")
    target = bytes.fromhex("55 44 33 22 11 00 00")
    assert _pairing_status(unrelated, 0, target) is None
    wrong_bearer = bytes.fromhex("01 00 00 00 0a 00 19 00 00 55 44 33 22 11 00 01")
    assert _pairing_status(wrong_bearer, 0, target) is None


def test_agent_rejects_foreign_devices_unknown_services_and_passkeys() -> None:
    agent = ClassicAgent(PATH)
    agent.RequestConfirmation(PATH, 123456)
    agent.RequestAuthorization(PATH)
    agent.AuthorizeService(PATH, "00009990-0000-1000-8000-00805f9b34fb")
    with pytest.raises(DBusError):
        agent.RequestConfirmation(FOREIGN, 123456)
    with pytest.raises(DBusError):
        agent.RequestAuthorization(FOREIGN)
    with pytest.raises(DBusError):
        agent.AuthorizeService(PATH, "00001101-0000-1000-8000-00805f9b34fb")
    with pytest.raises(DBusError):
        agent.RequestPinCode(PATH)
    with pytest.raises(DBusError):
        agent.RequestPasskey(PATH)


async def test_new_bond_restores_global_state_and_trusts_scale() -> None:
    bus = FakeBus()

    async def pair(_path):
        assert bus.state[(CONTROLLER, ADAPTER, "Pairable")] is True
        bus.state[(PATH, CLASSIC, "Paired")] = True
        bus.state[(PATH, CLASSIC, "Bonded")] = True

    with (
        patch("custom_components.withings_local.classic_pairing.MessageBus", return_value=bus),
        patch("custom_components.withings_local.classic_pairing._pair_br_edr", new_callable=AsyncMock, side_effect=pair) as mgmt,
    ):
        assert await ClassicPairingSession(PATH).pair() is False
    mgmt.assert_awaited_once_with(PATH)
    assert bus.state[(PATH, DEVICE, "Trusted")] is True
    assert bus.state[(CONTROLLER, ADAPTER, "Pairable")] is False
    assert bus.state[(CONTROLLER, ADAPTER, "PairableTimeout")] == 0
    assert bus.state[(CONTROLLER, ADAPTER, "Discoverable")] is False
    assert bus.calls.index(("org.bluez.AgentManager1", "UnregisterAgent")) > bus.calls.index(
        ("org.bluez.AgentManager1", "RequestDefaultAgent")
    )
    assert len(bus.exports) == len(bus.unexports) == 1
    assert bus.closed


async def test_existing_classic_bond_skips_all_pairing_operations() -> None:
    bus = FakeBus(bonded=True)
    with (
        patch("custom_components.withings_local.classic_pairing.MessageBus", return_value=bus),
        patch("custom_components.withings_local.classic_pairing._pair_br_edr", new_callable=AsyncMock) as mgmt,
    ):
        assert await ClassicPairingSession(PATH).pair() is True
    mgmt.assert_not_awaited()
    assert ("org.bluez.AgentManager1", "RegisterAgent") not in bus.calls
    assert bus.state[(PATH, DEVICE, "Trusted")] is True
    assert bus.state[(CONTROLLER, ADAPTER, "Pairable")] is False
    assert bus.closed


@pytest.mark.parametrize("bonded", [False, True])
async def test_existing_bond_inspection_is_read_only(bonded: bool) -> None:
    bus = FakeBus(bonded=bonded)
    with patch("custom_components.withings_local.classic_pairing.MessageBus", return_value=bus):
        assert await ClassicPairingSession(PATH).bonded() is bonded
    assert bus.closed
    assert bus.state[(PATH, DEVICE, "Trusted")] is False
    assert not bus.exports
    assert all(member == "Get" for _interface, member in bus.calls)


async def test_pairing_failure_restores_adapter_and_unregisters_temporary_agent() -> None:
    bus = FakeBus()
    with (
        patch("custom_components.withings_local.classic_pairing.MessageBus", return_value=bus),
        patch(
            "custom_components.withings_local.classic_pairing._pair_br_edr",
            new_callable=AsyncMock,
            side_effect=PairingFailed("connect failed"),
        ),
    ):
        with pytest.raises(PairingFailed, match="connect failed"):
            await ClassicPairingSession(PATH).pair()
    assert bus.state[(CONTROLLER, ADAPTER, "Pairable")] is False
    assert bus.state[(CONTROLLER, ADAPTER, "PairableTimeout")] == 0
    assert ("org.bluez.AgentManager1", "UnregisterAgent") in bus.calls
    assert bus.closed


async def test_existing_pairable_adapter_is_left_alone() -> None:
    bus = FakeBus(pairable=True)
    with (
        patch("custom_components.withings_local.classic_pairing.MessageBus", return_value=bus),
        patch("custom_components.withings_local.classic_pairing._pair_br_edr", new_callable=AsyncMock) as mgmt,
    ):
        with pytest.raises(PairingFailed, match="already pairable"):
            await ClassicPairingSession(PATH).pair()
    mgmt.assert_not_awaited()
    assert bus.state[(CONTROLLER, ADAPTER, "Pairable")] is True
    assert ("org.bluez.AgentManager1", "RegisterAgent") not in bus.calls
    assert bus.closed
