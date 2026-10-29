import asyncio
from datetime import UTC, datetime
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.withings_local.const import CONF_KL, DOMAIN
from custom_components.withings_local.coordinator import WithingsScaleCoordinator, has_new_data, latest_per_user
from custom_components.withings_local.withings_wpp.client import RfcommWppSession, SyncResult
from custom_components.withings_local.withings_wpp.measures import Measure, MeasureGroup

from .conftest import ADDRESS, SETUP_UUID, TEST_KL, WBS06_ADDRESS, WBS06_SETUP_UUID, WBS06_UUID, service_info

ALICE, BOB = 1001, 1002  # synthetic user IDs


@pytest.fixture(autouse=True)
def no_initial_bluetooth_presence():
    """Coordinator tests supply their own advertisements, without global BlueZ state."""
    with (
        patch(
            "homeassistant.components.bluetooth.update_coordinator.async_address_present",
            return_value=False,
        ),
        patch(
            "custom_components.withings_local.coordinator.bluetooth.async_ble_device_from_address",
            return_value=None,
        ),
    ):
        yield


def test_has_new_data_only_for_installed_advert() -> None:
    assert has_new_data(service_info())
    assert not has_new_data(service_info(uuid=SETUP_UUID, mfr=b"\x00\x00\x00\x00"))


def test_has_new_data_from_manufacturer_data_alone() -> None:
    # Passive scanners may miss the scan response that carries the service UUID.
    assert has_new_data(service_info(uuid=None, mfr=b"\x12\x34\x56\x78"))
    assert not has_new_data(service_info(uuid=None, mfr=b"\x00\x00\x00\x00"))
    assert not has_new_data(service_info(uuid=None, mfr=None))


def test_wbs06_advertisement_and_setup_mode() -> None:
    from custom_components.withings_local.coordinator import is_setup_mode

    installed = service_info(name="Body 5E", address=WBS06_ADDRESS, uuid=WBS06_UUID, mfr=b"\x4b\xc2\x9d\x15")
    setup = service_info(name="Body 5E", address=WBS06_ADDRESS, uuid=WBS06_SETUP_UUID, mfr=b"\0\0\0\0")
    assert is_setup_mode(setup, 7)
    assert not is_setup_mode(installed, 7)


def test_latest_per_user_merges_and_separates_users() -> None:
    t1, t2, t3 = (datetime(2026, 1, d, tzinfo=UTC) for d in (1, 2, 3))
    groups = [
        MeasureGroup(t1, [ALICE], 0, 0, [Measure(1, 80.0)]),
        MeasureGroup(t2, [ALICE], 0, 0, [Measure(1, 70.0), Measure(8, 14.0)]),
        MeasureGroup(t2, [ALICE], 0, 0, [Measure(11, 60.0)]),  # heart rate arrives as its own group
        MeasureGroup(t2, [ALICE], 0, 0, [Measure(1, 70.0)]),  # the whole store is re-sent every sync
        MeasureGroup(t3, [BOB], 0, 0, [Measure(1, 90.0)]),
        MeasureGroup(t3, [], 0, 0, [Measure(1, 55.0)]),  # guest: no user ID
    ]
    latest = latest_per_user(groups)
    assert set(latest) == {ALICE, BOB, 0}
    assert latest[ALICE].measured_at == t2
    assert latest[ALICE].values == {"weight": 70.0, "fat_mass": 14.0, "heart_rate": 60.0, "fat_ratio": 20.0}
    assert latest[BOB].values == {"weight": 90.0}
    assert latest[0].values == {"weight": 55.0}
    assert latest_per_user([]) == {}


async def test_retries_during_window_after_failure(hass, mock_bluetooth) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.withings_local.const import CONF_KL, DOMAIN
    from custom_components.withings_local.coordinator import WithingsScaleCoordinator

    from .conftest import ADDRESS, TEST_KL

    entry = MockConfigEntry(domain=DOMAIN, title="Body Smart", data={"address": ADDRESS, CONF_KL: TEST_KL})
    coordinator = WithingsScaleCoordinator(hass, entry)
    info = service_info()
    assert coordinator._needs_poll(info, None)  # noqa: SLF001
    assert not coordinator._needs_poll(info, 5)  # noqa: SLF001 - recent successful sync: wait
    coordinator.last_poll_successful = False
    assert coordinator._needs_poll(info, 5)  # noqa: SLF001 - recent failure: retry now


async def test_wbs06_does_not_initiate_le_sync_after_weigh_in(hass, mock_bluetooth) -> None:
    """Only incoming Classic RFCOMM reads WBS06 measurements."""
    from custom_components.withings_local.const import BODY_MODEL_ID, CONF_MODEL_ID

    entry = MockConfigEntry(
        domain=DOMAIN, title="Body",
        data={"address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID},
    )
    coordinator = WithingsScaleCoordinator(hass, entry)
    info = service_info(name="Body", address=WBS06_ADDRESS, uuid=WBS06_UUID)
    with (
        patch("custom_components.withings_local.coordinator.establish_connection") as connect,
        patch("custom_components.withings_local.coordinator.read_measurements") as read,
    ):
        assert not coordinator._needs_poll(info, None)  # noqa: SLF001
        assert not coordinator._needs_poll(info, 1000)  # noqa: SLF001
        with pytest.raises(RuntimeError, match="incoming Classic RFCOMM"):
            await coordinator._async_sync(info)  # noqa: SLF001
    connect.assert_not_called()
    read.assert_not_called()


def test_setup_mode_detected_despite_merged_weigh_in_data() -> None:
    from custom_components.withings_local.coordinator import is_setup_mode

    from .conftest import INSTALLED_UUID

    merged = service_info(uuid=SETUP_UUID, mfr=b"\x12\x34\x56\x78")  # stale weigh-in bytes
    merged.service_uuids.append(INSTALLED_UUID)  # stale weigh-in UUID
    assert is_setup_mode(merged)
    assert not is_setup_mode(service_info())  # only a weigh-in advertisement


async def test_sync_ignores_readings_from_unverified_scale(hass, mock_bluetooth) -> None:
    """The device fails our challenge: nothing is recorded."""
    entry = MockConfigEntry(domain=DOMAIN, title="Body Smart", data={"address": ADDRESS, CONF_KL: TEST_KL})
    entry.add_to_hass(hass)  # a successful sync stores the users it saw on the entry
    coordinator = WithingsScaleCoordinator(hass, entry)
    when = datetime(2026, 1, 2, tzinfo=UTC)
    group = MeasureGroup(when, [ALICE], 0, 0, [Measure(1, 70.0)])

    def result(verified: bool) -> SyncResult:
        return SyncResult(True, verified, 2, 1311, [group], battery=80)

    client = MagicMock(disconnect=AsyncMock())
    with (
        patch("custom_components.withings_local.coordinator.establish_connection", AsyncMock(return_value=client)),
        patch("custom_components.withings_local.coordinator.WppSession"),
        patch("custom_components.withings_local.coordinator.read_measurements", AsyncMock(return_value=result(False))),
    ):
        data = await coordinator._async_sync(service_info())  # noqa: SLF001
    assert data.users == {} and data.battery is None and data.last_sync is None

    with (
        patch("custom_components.withings_local.coordinator.establish_connection", AsyncMock(return_value=client)),
        patch("custom_components.withings_local.coordinator.WppSession"),
        patch("custom_components.withings_local.coordinator.read_measurements", AsyncMock(return_value=result(True))),
    ):
        data = await coordinator._async_sync(service_info())  # noqa: SLF001
    assert data.users[ALICE].values == {"weight": 70.0} and data.battery == 80


async def test_wbs06_only_registers_incoming_profiles(hass, mock_bluetooth) -> None:
    """WBS13 keeps its existing BLE-only lifecycle."""
    from custom_components.withings_local.const import BODY_MODEL_ID, CONF_MODEL_ID

    entry = MockConfigEntry(domain=DOMAIN, title="Body", data={
        "address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID,
    })
    coordinator = WithingsScaleCoordinator(hass, entry)
    server = MagicMock(start=AsyncMock(), stop=AsyncMock(), remove=AsyncMock())
    server.addresses = {WBS06_ADDRESS}
    server.remove.side_effect = lambda address: server.addresses.remove(address)
    with patch("custom_components.withings_local.coordinator.Wbs06RfcommServer", return_value=server) as factory:
        await coordinator.async_start_classic()
        await coordinator.async_start_classic()  # idempotent
        factory.assert_called_once()
        factory.assert_called_once_with()
        server.add.assert_called_once_with(WBS06_ADDRESS, coordinator._handle_classic_connection)
        server.start.assert_awaited_once()
        await coordinator.async_stop_classic()
        server.remove.assert_awaited_once_with(WBS06_ADDRESS)
        server.stop.assert_awaited_once()
        await coordinator.async_stop_classic()
        server.stop.assert_awaited_once()

    wbs13 = MockConfigEntry(domain=DOMAIN, title="Body Smart", data={"address": ADDRESS, CONF_KL: TEST_KL})
    coordinator13 = WithingsScaleCoordinator(hass, wbs13)
    with patch("custom_components.withings_local.coordinator.Wbs06RfcommServer") as factory:
        await coordinator13.async_start_classic()
        factory.assert_not_called()


async def test_two_wbs06_coordinators_share_one_listener_until_last_unload(hass, mock_bluetooth):
    from custom_components.withings_local.const import BODY_MODEL_ID, CONF_MODEL_ID

    a = MockConfigEntry(domain=DOMAIN, title="Body A", data={
        "address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID,
    })
    b_address = "66:77:88:99:AA:BB"
    b = MockConfigEntry(domain=DOMAIN, title="Body B", data={
        "address": b_address, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID,
    })
    first, second = WithingsScaleCoordinator(hass, a), WithingsScaleCoordinator(hass, b)
    server = MagicMock(start=AsyncMock(), stop=AsyncMock(), remove=AsyncMock())
    addresses = set()
    server.add.side_effect = lambda addr, handler: addresses.add(addr)
    server.remove.side_effect = lambda addr: addresses.remove(addr)
    server.addresses = addresses
    with patch("custom_components.withings_local.coordinator.Wbs06RfcommServer", return_value=server) as factory:
        await asyncio.gather(first.async_start_classic(), second.async_start_classic())
        factory.assert_called_once_with()
        assert addresses == {WBS06_ADDRESS, b_address}
        server.start.assert_awaited_once()
        await first.async_stop_classic()
        server.stop.assert_not_awaited()
        assert addresses == {b_address}
        await second.async_stop_classic()
        server.stop.assert_awaited_once()


async def test_wbs06_integration_cannot_load_without_classic_listener(hass, mock_bluetooth) -> None:
    """No silent BLE fallback: WBS06 needs its RFCOMM service to receive readings."""
    from homeassistant.exceptions import ConfigEntryNotReady

    from custom_components.withings_local import async_setup_entry
    from custom_components.withings_local.const import BODY_MODEL_ID, CONF_MODEL_ID

    entry = MockConfigEntry(
        domain=DOMAIN, title="Body",
        data={"address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.withings_local.coordinator.Wbs06RfcommServer"
    ) as server:
        server.return_value.start = AsyncMock(side_effect=RuntimeError("BlueZ unavailable"))
        with pytest.raises(ConfigEntryNotReady, match="Classic RFCOMM listener unavailable"):
            await async_setup_entry(hass, entry)
    server.return_value.start.assert_awaited_once()


@pytest.mark.enable_socket
async def test_wbs06_classic_measurements_require_verified_challenge(hass, mock_bluetooth) -> None:
    from custom_components.withings_local.const import BODY_MODEL_ID, CONF_MODEL_ID

    entry = MockConfigEntry(domain=DOMAIN, title="Body", data={
        "address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID,
    })
    entry.add_to_hass(hass)
    coordinator = WithingsScaleCoordinator(hass, entry)
    measured = datetime(2026, 1, 2, tzinfo=UTC)
    group = MeasureGroup(measured, [ALICE], 0, 0, [Measure(1, 70.0)])
    local, remote = socket.socketpair()
    local.setblocking(False)
    try:
        read = AsyncMock(return_value=SyncResult(True, False, None, 1, [group]))
        with patch("custom_components.withings_local.coordinator.read_measurements", read):
            await coordinator._handle_classic_connection(local)  # noqa: SLF001
        assert coordinator.data.users == {}
        assert coordinator.data.last_sync is None
        assert read.call_args.args[:2] == (None, TEST_KL)
        assert read.call_args.args[2].sync_ok is False
        assert read.call_args.kwargs == {"app_version": 8050004, "authentication_timeout": 15.0}

        local2, remote2 = socket.socketpair()
        local2.setblocking(False)
        try:
            read = AsyncMock(return_value=SyncResult(True, True, 2, 1234, [group], battery=77))
            with patch("custom_components.withings_local.coordinator.read_measurements", read):
                await coordinator._handle_classic_connection(local2)  # noqa: SLF001
            assert coordinator.data.users[ALICE].values["weight"] == 70.0
            assert coordinator.data.battery == 77
            assert coordinator.data.last_sync is not None
            assert read.call_args.args[2].sync_ok is False
            assert read.call_args.kwargs == {"app_version": 8050004, "authentication_timeout": 15.0}
        finally:
            remote2.close()
    finally:
        remote.close()


@pytest.mark.enable_socket
async def test_wbs06_classic_probe_timeout_is_logged_without_measurement(hass, mock_bluetooth, caplog) -> None:
    """Silence on the RFCOMM stream is diagnostic, not an authenticated sync."""
    from custom_components.withings_local.const import BODY_MODEL_ID, CONF_MODEL_ID

    entry = MockConfigEntry(domain=DOMAIN, title="Body", data={
        "address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID,
    })
    coordinator = WithingsScaleCoordinator(hass, entry)
    local, remote = socket.socketpair()
    local.setblocking(False)
    try:
        async def timeout_read(_ble, _kl, session, **_options):
            assert isinstance(session, RfcommWppSession)
            session.tx_bytes = 25
            session.rx_bytes = 0
            session.rx_chunks = 0
            raise TimeoutError

        with patch("custom_components.withings_local.coordinator.read_measurements", timeout_read):
            await coordinator._handle_classic_connection(local)  # noqa: SLF001
        assert coordinator.data.users == {}
        assert coordinator.data.last_sync is None
        assert "sent 25 bytes, received 0 bytes in 0 chunks" in caplog.text
    finally:
        remote.close()
