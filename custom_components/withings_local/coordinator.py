"""Connect to the scale after a weigh-in and read its stored measurements."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
import asyncio
from datetime import datetime
import logging
import socket
import time

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from .withings_wpp.advertisement import WithingsUuid
from .withings_wpp.client import RfcommWppSession, SyncResult, WppSession, read_measurements
from .withings_wpp.measures import MeasureGroup, merge_by_time

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothScanningMode, BluetoothServiceInfoBleak
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    BODY_MODEL_ID,
    BODY_SMART_MODEL_ID,
    CONF_KL,
    CONF_MODEL_ID,
    DOMAIN,
    OPT_KNOWN_USERS,
    POLL_COOLDOWN_SECONDS,
    SESSION_TIMEOUT_SECONDS,
)
from .rfcomm import Wbs06RfcommServer

_LOGGER = logging.getLogger(__name__)

_CLASSIC_SERVER_KEY = "wbs06_classic_server"
_CLASSIC_LOCK_KEY = "wbs06_classic_lock"

type WithingsLocalConfigEntry = ConfigEntry[WithingsScaleCoordinator]

# WBS06 requires this APP_PROBE version during Classic synchronization.
WBS06_RFCOMM_APP_VERSION = 8050004
WBS06_RFCOMM_AUTH_TIMEOUT = 15.0


@dataclass
class WeighIn:
    """One person's latest weigh-in."""

    measured_at: datetime
    values: dict[str, float]


@dataclass
class ScaleData:
    """What the last sync read: each user's latest weigh-in plus scale-level state."""

    users: dict[int, WeighIn] = field(default_factory=dict)
    firmware: int | None = None
    battery: int | None = None
    last_sync: datetime | None = None


def latest_per_user(groups: list[MeasureGroup]) -> dict[int, WeighIn]:
    """Each user's newest weigh-in (merged across same-timestamp groups), plus a derived fat ratio.

    User 0 collects weigh-ins the scale couldn't attribute to anyone.
    """
    latest: dict[int, WeighIn] = {}
    for g in merge_by_time(groups):  # oldest first, so later ones win
        values = {m.name: m.value for m in g.measures}
        if (weight := values.get("weight")) and (fat := values.get("fat_mass")) is not None:
            values.setdefault("fat_ratio", round(fat / weight * 100, 2))
        latest[g.user_id] = WeighIn(g.time, values)
    return latest


WITHINGS_COMPANY_ID = 0x03FF


def has_new_data(service_info: BluetoothServiceInfoBleak) -> bool:
    """True when the scale advertises as an installed device (after a weigh-in).

    Two signals, because passive scanners may not receive the scan response:
    - the service UUID tail carries the scale's MAC (all zeros in setup/button sessions);
    - the Withings manufacturer data is non-zero (all zeros in setup sessions).
    """
    for uuid in service_info.service_uuids:
        w = WithingsUuid.parse(uuid)
        if w and w.model_id == BODY_SMART_MODEL_ID and w.tail.strip("0"):
            return True
    mfr = service_info.manufacturer_data.get(WITHINGS_COMPANY_ID)
    return bool(mfr) and any(mfr)


def _is_bonded(device) -> bool:
    """BlueZ reports Paired on the device object; unknown elsewhere (assume bonded)."""
    details = getattr(device, "details", None)
    if not isinstance(details, dict) or "props" not in details:
        return True
    return bool(details["props"].get("Paired"))


def is_setup_mode(service_info: BluetoothServiceInfoBleak, model_id: int = BODY_SMART_MODEL_ID) -> bool:
    """True when the scale advertises in setup mode (button held 3 s): its service UUID then has
    an all-zero tail. Home Assistant merges a device's advertisement data with earlier packets,
    so weigh-in data (MAC tail, manufacturer bytes) may still be present; only look for the
    setup-mode UUID."""
    return any(
        (w := WithingsUuid.parse(u)) and w.model_id == model_id and not w.tail.strip("0")
        for u in service_info.service_uuids
    )


class WithingsScaleCoordinator(ActiveBluetoothDataUpdateCoordinator[ScaleData]):
    """Poll the scale whenever it advertises new measurements."""

    def __init__(self, hass: HomeAssistant, entry: WithingsLocalConfigEntry) -> None:
        self._kl: str = entry.data[CONF_KL]
        self.model_id: int = entry.data.get(CONF_MODEL_ID, BODY_SMART_MODEL_ID)
        self._entry = entry
        self.config_entry_id = entry.entry_id
        self.title = entry.title
        # Users seen so far, in order of first appearance; persisted so their entities exist after a restart.
        self.known_users: list[int] = list(entry.options.get(OPT_KNOWN_USERS, []))
        self._new_user_listeners: list[Callable[[list[int]], None]] = []
        self._pairing_requested = False
        self._classic_server: Wbs06RfcommServer | None = None
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            address=entry.data[CONF_ADDRESS],
            mode=BluetoothScanningMode.ACTIVE,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_sync,
            connectable=True,
        )
        self.data = ScaleData()

    async def async_start_classic(self) -> None:
        """Share one native RFCOMM profile pair across all configured WBS06 scales."""
        if self.model_id != BODY_MODEL_ID or self._classic_server is not None:
            return
        data = self.hass.data.setdefault(DOMAIN, {})
        lock = data.setdefault(_CLASSIC_LOCK_KEY, asyncio.Lock())
        async with lock:
            if self._classic_server is not None:
                return
            server = data.get(_CLASSIC_SERVER_KEY)
            if server is None:
                server = Wbs06RfcommServer()
                server.add(self.address, self._handle_classic_connection)
                await server.start()
                data[_CLASSIC_SERVER_KEY] = server
            else:
                server.add(self.address, self._handle_classic_connection)
            self._classic_server = server

    async def async_stop_classic(self) -> None:
        if self._classic_server is None:
            return
        data = self.hass.data[DOMAIN]
        async with data[_CLASSIC_LOCK_KEY]:
            server = self._classic_server
            if server is None:
                return
            self._classic_server = None
            await server.remove(self.address)
            if not server.addresses:
                try:
                    await server.stop()
                finally:
                    data.pop(_CLASSIC_SERVER_KEY, None)

    async def _handle_classic_connection(self, sock: socket.socket) -> None:
        """Authenticate before processing a scale-initiated RFCOMM session.

        Initial Classic support deliberately skips SYNC_OK so it cannot
        change the state of measurements before the transport is validated
        against this model.
        """
        session = RfcommWppSession(sock, sync_ok=False)
        try:
            try:
                async with asyncio.timeout(SESSION_TIMEOUT_SECONDS):
                    result = await read_measurements(
                        None, self._kl, session,
                        app_version=WBS06_RFCOMM_APP_VERSION,
                        authentication_timeout=WBS06_RFCOMM_AUTH_TIMEOUT,
                    )
            except TimeoutError:
                _LOGGER.warning(
                    "%s: RFCOMM WPP timed out (sent %d bytes, received %d bytes in %d chunks)",
                    self.title, session.tx_bytes, session.rx_bytes, session.rx_chunks,
                )
                return
            if not result.authenticated or not result.scale_verified:
                _LOGGER.warning("%s: rejected unauthenticated RFCOMM measurements", self.title)
                return
            # ActiveBluetoothDataUpdateCoordinator is not a general
            # DataUpdateCoordinator: publish through its listener interface.
            self.data = self._merge_result(result)
            self._available = True
            self._last_poll = time.monotonic()
            self.last_poll_successful = True
            self.async_update_listeners()
        finally:
            await session.close()

    @callback
    def _async_request_pairing(self) -> None:
        """Ask the user to pair (reauth flow). HA keeps a single flow per entry."""
        if not self._pairing_requested:
            self._pairing_requested = True
            self._entry.async_start_reauth(self.hass)

    @callback
    def async_add_new_user_listener(self, listener: Callable[[list[int]], None]) -> CALLBACK_TYPE:
        """Call `listener` with the IDs of users seen for the first time."""
        self._new_user_listeners.append(listener)
        return lambda: self._new_user_listeners.remove(listener)

    @callback
    def _register_users(self, user_ids) -> None:
        new = [u for u in user_ids if u not in self.known_users]
        if not new:
            return
        self.known_users.extend(new)
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, OPT_KNOWN_USERS: self.known_users}
        )
        for listener in list(self._new_user_listeners):
            listener(new)

    @callback
    def _needs_poll(self, service_info: BluetoothServiceInfoBleak, seconds_since_last_poll: float | None) -> bool:
        if self.model_id == BODY_MODEL_ID:
            # WBS06 initiates Classic RFCOMM after weigh-ins.
            # Do not race it with a BLE connection or trigger implicit SMP.
            return False
        new_data = has_new_data(service_info)
        # The cooldown only avoids re-reading after a successful sync; after a failure, retry while
        # the scale is still advertising (its window is short).
        due = (
            seconds_since_last_poll is None
            or seconds_since_last_poll >= POLL_COOLDOWN_SECONDS
            or not self.last_poll_successful
        )
        _LOGGER.debug(
            "%s: advertisement via %s (rssi %s, connectable %s, uuids %d, withings mfr data %s): new data %s, poll due %s",
            self.title,
            service_info.source,
            service_info.rssi,
            service_info.connectable,
            len(service_info.service_uuids),
            "present" if WITHINGS_COMPANY_ID in service_info.manufacturer_data else "absent",
            new_data,
            due,
        )
        return new_data and due

    async def _async_sync(self, service_info: BluetoothServiceInfoBleak) -> ScaleData:
        if self.model_id == BODY_MODEL_ID:
            raise RuntimeError("WBS06 readings arrive through incoming Classic RFCOMM")
        _LOGGER.info(
            "%s: weigh-in advertised, connecting via %s (rssi %s)", self.title, service_info.source, service_info.rssi
        )
        device = bluetooth.async_ble_device_from_address(self.hass, service_info.address, connectable=True)
        started = time.monotonic()
        step = "connecting"
        session: WppSession | None = None

        def on_disconnect(_client) -> None:
            _LOGGER.debug("%s: scale disconnected after %.1fs", self.title, time.monotonic() - started)
            if session is not None:
                session.mark_disconnected()

        def fresh_device():
            # Retries use the newest BLEDevice (the best adapter may change between attempts).
            return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True) or (
                device or service_info.device
            )

        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                device or service_info.device,
                self.title,
                disconnected_callback=on_disconnect,
                max_attempts=3,
                ble_device_callback=fresh_device,
            )
            step = "reading"
            _LOGGER.debug("%s: connected after %.1fs", self.title, time.monotonic() - started)
            try:
                # A stuck session must never block later syncs.
                session = WppSession(client)
                async with asyncio.timeout(SESSION_TIMEOUT_SECONDS):
                    result = await read_measurements(client, self._kl, session)
            finally:
                await client.disconnect()
        except Exception as err:
            # HA's coordinator only logs the first failure in a row; log every one so users can debug range issues.
            # BlueZ's aggregate Paired flag can be true for WBS06's Classic
            # bearer while its LE bearer is still unbonded.
            needs_pairing = (
                step == "reading"
                and "subscrib" in str(err).lower()
                and not _is_bonded(device or service_info.device)
            )
            if needs_pairing:
                hint = " The scale needs to be paired with Home Assistant; a repair notification will guide you."
                self._async_request_pairing()
            elif step == "connecting":
                hint = " " + bluetooth.async_address_reachability_diagnostics(
                    self.hass, self.address, bluetooth.BluetoothReachabilityIntent.CONNECTION
                )
            else:
                hint = ""
            _LOGGER.warning(
                "%s: sync failed while %s after %.1fs via %s (rssi %s): %s: %s%s",
                self.title,
                step,
                time.monotonic() - started,
                service_info.source,
                service_info.rssi,
                type(err).__name__,
                err,
                hint,
            )
            raise
        return self._merge_result(result)

    def _merge_result(self, result: SyncResult) -> ScaleData:
        """Apply verified WPP readings identically for BLE and incoming Classic."""
        if not result.authenticated:
            _LOGGER.debug("%s: session without authentication (button press?), nothing to read", self.title)
            return self.data
        if not result.scale_verified:
            # A device that fails our challenge doesn't know kl: don't record what it sends
            # (anyone in range can copy the advertisement). A real scale always passes.
            _LOGGER.warning("%s: the device did not prove it knows the key; ignoring its readings", self.title)
            return self.data

        users = dict(self.data.users)
        for user_id, weigh_in in latest_per_user(result.groups).items():
            if user_id not in users or weigh_in.measured_at >= users[user_id].measured_at:
                users[user_id] = weigh_in
        data = ScaleData(
            users=users,
            firmware=result.firmware or self.data.firmware,
            battery=result.battery if result.battery is not None else self.data.battery,
            last_sync=dt_util.utcnow(),
        )
        self._register_users(sorted(users, key=lambda u: users[u].measured_at))
        for g in merge_by_time(result.groups):
            _LOGGER.debug("%s: stored weigh-in %s, user %s, %d values", self.title, g.time, g.user_id, len(g.measures))
        _LOGGER.info(
            "%s: sync done: %d weigh-in(s) stored on the scale, %d user(s)", self.title, len(result.groups), len(users)
        )
        return data
