"""Explicit, device-scoped BR/EDR pairing for WBS06.

The scale initiates Classic RFCOMM after a weigh-in, but accepts a new
bond only in physical setup mode. BlueZ Device1.Pair() cannot select a
specific bearer on dual-mode devices, so use Linux's documented
management Pair Device operation with the BR/EDR address type.

This module never unpairs, resets, or modifies LE keys. Pairable is
enabled only for one user-requested attempt and restored afterward.
"""

import asyncio
import logging
import re
import secrets
import socket
import struct

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, method

from .pairing import PairingFailed, PairingUnsupported
from .rfcomm import WBS06_RFCOMM_UUIDS

_LOGGER = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
CLASSIC = "org.bluez.Bearer.BREDR1"
PROPERTIES = "org.freedesktop.DBus.Properties"
AGENT_MANAGER = "org.bluez.AgentManager1"

MGMT_PAIR = 0x0019
MGMT_CANCEL_PAIR = 0x001A
MGMT_COMMAND_COMPLETE = 0x0001
MGMT_COMMAND_STATUS = 0x0002
MGMT_CONTROL_CHANNEL = 3
MGMT_NO_DEVICE = 0xFFFF
NO_INPUT_NO_OUTPUT = 3
PAIR_TIMEOUT = 35
PAIRABLE_FAILSAFE_SECONDS = PAIR_TIMEOUT + 20
BLUEZ_PATH = re.compile(r"^/org/bluez/hci([0-9]+)/dev_((?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2})$")


def _rejected() -> None:
    raise DBusError("org.bluez.Error.Rejected", "not authorized for this scale")


class ClassicAgent(ServiceInterface):
    """Just Works pairing agent that accepts only the selected scale."""

    def __init__(self, device_path: str) -> None:
        super().__init__("org.bluez.Agent1")
        self.device_path = device_path

    def _require_scale(self, device: str) -> None:
        if device != self.device_path:
            _rejected()

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821 - D-Bus signatures
        self._require_scale(device)

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: F821 - D-Bus signatures
        self._require_scale(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821 - D-Bus signatures
        self._require_scale(device)
        if uuid.lower() not in WBS06_RFCOMM_UUIDS:
            _rejected()

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: F821 - D-Bus signatures
        _rejected()

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821 - D-Bus signatures
        _rejected()

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821 - D-Bus signatures
        _rejected()

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821 - D-Bus signatures
        _rejected()

    @method()
    def Cancel(self):
        return

    @method()
    def Release(self):
        return


def _management_packet(device_path: str, operation: int) -> tuple[int, bytes]:
    """Encode BR/EDR-only pairing without an LE address or an unpair operation."""
    match = BLUEZ_PATH.fullmatch(device_path)
    if match is None or operation not in (MGMT_PAIR, MGMT_CANCEL_PAIR):
        raise ValueError("invalid Classic Bluetooth pairing target")
    index = int(match[1])
    address = bytes.fromhex(match[2].replace("_", ""))[::-1]
    payload = address + (bytes((0, NO_INPUT_NO_OUTPUT)) if operation == MGMT_PAIR else b"\x00")
    return index, struct.pack("<HHH", operation, index, len(payload)) + payload


def _pairing_status(event: bytes, controller: int, target: bytes) -> tuple[bool, int] | None:
    """Match a management pairing response to the requested controller and device."""
    if len(event) < 9:
        return None
    event_type, index, size = struct.unpack_from("<HHH", event)
    if index != controller or len(event) < size + 6 or size < 3:
        return None
    if event_type not in (MGMT_COMMAND_COMPLETE, MGMT_COMMAND_STATUS):
        return None
    operation, status = struct.unpack_from("<HB", event, 6)
    if operation != MGMT_PAIR:
        return None
    if event_type == MGMT_COMMAND_COMPLETE:
        if size != 10 or event[9:16] != target:
            return None
    elif size != 3:
        return None
    return event_type == MGMT_COMMAND_COMPLETE, status


async def _pair_br_edr(device_path: str, timeout: float = PAIR_TIMEOUT) -> None:
    """Send exactly one Linux management Pair Device command for BR/EDR."""
    index, request = _management_packet(device_path, MGMT_PAIR)
    _, cancel = _management_packet(device_path, MGMT_CANCEL_PAIR)
    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    except OSError as err:
        raise PairingUnsupported("pairing_not_available") from err
    try:
        sock.bind((MGMT_NO_DEVICE, MGMT_CONTROL_CHANNEL))
        sock.setblocking(False)
    except OSError as err:
        sock.close()
        raise PairingUnsupported("pairing_not_available") from err
    completed = False
    try:
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(sock, request)
        end = loop.time() + timeout
        while loop.time() < end:
            try:
                event = await asyncio.wait_for(loop.sock_recv(sock, 4096), end - loop.time())
            except TimeoutError as err:
                raise PairingFailed("Classic Bluetooth pairing timed out; use scale setup mode") from err
            status = _pairing_status(event, index, request[6:13])
            if status is None:
                continue
            complete, code = status
            if not complete and code == 0:
                continue
            completed = True
            if code:
                raise PairingFailed(f"Classic Bluetooth pairing failed (BlueZ status 0x{code:02x})")
            return
        raise PairingFailed("Classic Bluetooth pairing timed out; use scale setup mode")
    finally:
        if not completed:
            # This is Cancel Pair Device, never Unpair Device. A direct send
            # avoids an interrupted async cleanup leaving a request running.
            try:
                sock.send(cancel)
            except OSError:
                _LOGGER.warning("Could not cancel pending Classic pairing attempt")
        sock.close()


class ClassicPairingSession:
    """One explicit WBS06 Classic pairing attempt through the native HA flow."""

    def __init__(self, device_path: str) -> None:
        if BLUEZ_PATH.fullmatch(device_path) is None:
            raise PairingUnsupported("pairing_not_available")
        self.device_path = device_path
        self.adapter_path = device_path.split("/dev_", 1)[0]
        self.agent_path = f"/org/home_assistant/withings_local/classic_{secrets.token_hex(5)}"

    async def _call(self, bus: MessageBus, path: str, interface: str, member: str, signature="", body=None):
        reply = await asyncio.wait_for(
            bus.call(Message(
                destination=BLUEZ, path=path, interface=interface, member=member,
                signature=signature, body=body or [],
            )), timeout=8,
        )
        if reply.message_type == MessageType.ERROR:
            raise PairingFailed(f"BlueZ {interface}.{member} failed ({reply.error_name})")
        return reply

    async def _get(self, bus: MessageBus, path: str, interface: str, name: str):
        reply = await self._call(bus, path, PROPERTIES, "Get", "ss", [interface, name])
        return reply.body[0].value

    async def _set(self, bus: MessageBus, name: str, value: int | bool) -> None:
        kind = "b" if isinstance(value, bool) else "u"
        await self._call(
            bus, self.adapter_path, PROPERTIES, "Set", "ssv",
            [ADAPTER, name, Variant(kind, value)],
        )

    async def _is_bonded(self, bus: MessageBus) -> bool:
        """Only a Classic bond suffices for the model's incoming RFCOMM link."""
        return await self._get(bus, self.device_path, CLASSIC, "Paired") is True and (
            await self._get(bus, self.device_path, CLASSIC, "Bonded")
        ) is True

    async def bonded(self) -> bool:
        """Inspect the stored Classic bond without changing trust or pairing state."""
        bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=False).connect()
        try:
            return await self._is_bonded(bus)
        finally:
            bus.disconnect()

    async def _trust(self, bus: MessageBus) -> None:
        """Preserve bonded dual-mode devices across BlueZ Classic disconnections."""
        if await self._get(bus, self.device_path, DEVICE, "Trusted") is True:
            return
        await self._call(
            bus, self.device_path, PROPERTIES, "Set", "ssv",
            [DEVICE, "Trusted", Variant("b", True)],
        )
        if await self._get(bus, self.device_path, DEVICE, "Trusted") is not True:
            raise PairingFailed("BlueZ did not retain trust for the scale")

    async def pair(self) -> bool:
        """Return True if already bonded; otherwise initiate one Classic pairing.

        Only a deliberate config-flow action calls this. A successful bond
        is persisted by BlueZ; global adapter state is temporary.
        """
        bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=False).connect()
        agent = ClassicAgent(self.device_path)
        exported = registered = changed_timeout = changed_pairable = False
        timeout_before: int | None = None
        cleanup_errors: list[str] = []
        try:
            if await self._is_bonded(bus):
                await self._trust(bus)
                return True
            if await self._get(bus, self.adapter_path, ADAPTER, "Discoverable") is not False:
                raise PairingFailed("adapter is discoverable; pairing policy is not safe to change")
            if await self._get(bus, self.adapter_path, ADAPTER, "Pairable") is not False:
                raise PairingFailed("adapter is already pairable; avoid conflicting pairing agents")
            timeout_before = await self._get(bus, self.adapter_path, ADAPTER, "PairableTimeout")
            bus.export(self.agent_path, agent)
            exported = True
            await self._call(
                bus, "/org/bluez", AGENT_MANAGER, "RegisterAgent", "os",
                [self.agent_path, "NoInputNoOutput"],
            )
            registered = True
            await self._call(
                bus, "/org/bluez", AGENT_MANAGER, "RequestDefaultAgent", "o",
                [self.agent_path],
            )
            changed_timeout = True
            await self._set(bus, "PairableTimeout", PAIRABLE_FAILSAFE_SECONDS)
            changed_pairable = True
            await self._set(bus, "Pairable", True)
            await _pair_br_edr(self.device_path)
            # BlueZ's D-Bus bearer property update follows command completion.
            for _ in range(12):
                if await self._is_bonded(bus):
                    await self._trust(bus)
                    return False
                await asyncio.sleep(0.25)
            raise PairingFailed("BlueZ did not persist the Classic bond")
        finally:
            if changed_pairable:
                try:
                    await self._set(bus, "Pairable", False)
                except Exception:  # noqa: BLE001 - cleanup must continue with other settings
                    cleanup_errors.append("Pairable")
            if changed_timeout and timeout_before is not None:
                try:
                    await self._set(bus, "PairableTimeout", timeout_before)
                except Exception:  # noqa: BLE001 - cleanup must continue with other settings
                    cleanup_errors.append("PairableTimeout")
            if registered:
                try:
                    await self._call(
                        bus, "/org/bluez", AGENT_MANAGER, "UnregisterAgent", "o",
                        [self.agent_path],
                    )
                except Exception:  # noqa: BLE001 - cleanup must release the D-Bus connection
                    cleanup_errors.append("agent")
            if exported:
                bus.unexport(self.agent_path, agent)
            bus.disconnect()
            if cleanup_errors:
                _LOGGER.error("WBS06 pairing cleanup failed for %s", ",".join(cleanup_errors))
                raise PairingFailed("could not restore Bluetooth pairing settings")
