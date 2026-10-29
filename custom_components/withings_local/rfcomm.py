"""WBS06-only incoming RFCOMM profiles managed by BlueZ.

WBS06 in Bluetooth-only mode initiates Classic connections
to its companion. BlueZ owns the listening sockets and SDP records;
Profile1 supplies each accepted file descriptor to this integration.
No pairing, adapter-mode changes, or system-wide SDP patches are needed.
"""

import asyncio
from collections.abc import Awaitable, Callable
import logging
import os
import re
import socket

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, method

_LOGGER = logging.getLogger(__name__)

BLUEZ = "org.bluez"
PROFILE_MANAGER = "org.bluez.ProfileManager1"
PROFILE_INTERFACE = "org.bluez.Profile1"
PROFILE_MANAGER_PATH = "/org/bluez"
PROFILE_PATH_PREFIX = "/org/home_assistant/withings_local/wbs06_profile_"

# Services used by the WBS06 for incoming RFCOMM connections.
WBS06_RFCOMM_UUIDS = (
    "00000001-e1ba-dfb7-c7f0-da2b149de289",
    "00009990-0000-1000-8000-00805f9b34fb",
)

type ConnectionHandler = Callable[[socket.socket], Awaitable[None]]


class Wbs06Profile(ServiceInterface):
    """One BlueZ Profile1 object, responsible for an exact service UUID."""

    def __init__(self, server: "Wbs06RfcommServer", path: str) -> None:
        super().__init__(PROFILE_INTERFACE)
        self._server = server
        self._path = path

    @method()
    def Release(self):  # noqa: N802 - BlueZ D-Bus API
        self._server.release(self._path)

    @method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):  # noqa: N802,F821,F722 - D-Bus signatures
        self._server.accept(device, fd)

    @method()
    def RequestDisconnection(self, device: "o"):  # noqa: N802,F821 - D-Bus signature
        self._server.disconnect(device)


def _device_address(device_path: str) -> str | None:
    """Return a validated BlueZ Classic address, independent of adapter number."""
    if not re.fullmatch(r"/org/bluez/hci[0-9]+/dev_(?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2}", device_path):
        return None
    return device_path.rsplit("/dev_", 1)[1].replace("_", ":").upper()


def _check(reply: Message, what: str) -> None:
    if reply.message_type == MessageType.ERROR:
        raise RuntimeError(f"{what}: {reply.error_name}")


class Wbs06RfcommServer:
    """One BlueZ profile pair shared by all WBS06 entries on this Home Assistant."""

    def __init__(self) -> None:
        self._handlers: dict[str, ConnectionHandler] = {}
        self._sockets: dict[str, socket.socket] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._registration_lock = asyncio.Lock()
        self._recovery_task: asyncio.Task | None = None
        self._bus: MessageBus | None = None
        self._exported: list[tuple[str, Wbs06Profile]] = []
        self._registered: list[str] = []
        self._stopping = False

    @property
    def addresses(self) -> frozenset[str]:
        """The scale addresses currently served by these shared SDP profiles."""
        return frozenset(self._handlers)

    def add(self, address: str, handler: ConnectionHandler) -> None:
        """Route one scale's connections without registering another SDP profile."""
        if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address):
            raise ValueError("expected a Bluetooth address")
        address = address.upper()
        if address in self._handlers:
            raise ValueError("WBS06 Classic handler already registered")
        self._handlers[address] = handler

    async def remove(self, address: str) -> None:
        """Stop one scale's in-flight sync while retaining other scale handlers."""
        address = address.upper()
        self._handlers.pop(address, None)
        self._close_connection(address)
        if (task := self._tasks.get(address)) is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def start(self) -> None:
        """Register two native SDP records and watch BlueZ daemon ownership."""
        if self._bus is not None:
            raise RuntimeError("RFCOMM profiles already started")
        self._stopping = False
        self._bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        try:
            for index in range(len(WBS06_RFCOMM_UUIDS)):
                path = f"{PROFILE_PATH_PREFIX}{index}"
                profile = Wbs06Profile(self, path)
                self._bus.export(path, profile)
                self._exported.append((path, profile))
            self._bus.add_message_handler(self._bluez_changed)
            reply = await self._bus.call(Message(
                destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus", member="AddMatch", signature="s",
                body=["type='signal',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'"],
            ))
            _check(reply, "watching BlueZ daemon")
            await self._register_profiles()
        except BaseException:
            await self.stop()
            raise
        _LOGGER.info("WBS06 RFCOMM listener registered for %d services", len(self._registered))

    async def _register_profiles(self) -> None:
        """Register missing BlueZ profiles; undo only this attempt on failure."""
        assert self._bus is not None
        async with self._registration_lock:
            added: list[str] = []
            try:
                for index, uuid in enumerate(WBS06_RFCOMM_UUIDS):
                    path = f"{PROFILE_PATH_PREFIX}{index}"
                    if path in self._registered:
                        continue
                    request = Message(
                        destination=BLUEZ, path=PROFILE_MANAGER_PATH, interface=PROFILE_MANAGER,
                        member="RegisterProfile", signature="osa{sv}",
                        body=[
                            path, uuid,
                            {
                                "Name": Variant("s", "Withings"),
                                "Role": Variant("s", "server"),
                                # For custom UUIDs BlueZ does not know the
                                # transport. Channel 0 means allocate a free
                                # RFCOMM server channel and publish it in SDP.
                                "Channel": Variant("q", 0),
                                # WPP authentication verifies the scale before
                                # accepting measurements.
                                "RequireAuthentication": Variant("b", False),
                                "RequireAuthorization": Variant("b", False),
                            },
                        ],
                    )
                    reply = await asyncio.wait_for(self._bus.call(request), timeout=10)
                    _check(reply, "registering WBS06 RFCOMM profile")
                    self._registered.append(path)
                    added.append(path)
            except BaseException:
                for path in reversed(added):
                    try:
                        await self._bus.call(Message(
                            destination=BLUEZ, path=PROFILE_MANAGER_PATH, interface=PROFILE_MANAGER,
                            member="UnregisterProfile", signature="o", body=[path],
                        ))
                    except Exception:
                        _LOGGER.warning("Could not roll back WBS06 profile registration", exc_info=True)
                    self._registered.remove(path)
                raise

    def _bluez_changed(self, message: Message) -> None:
        """A new org.bluez owner must receive fresh ProfileManager registrations."""
        if (
            self._stopping
            or message.message_type != MessageType.SIGNAL
            or message.interface != "org.freedesktop.DBus"
            or message.member != "NameOwnerChanged"
            or len(message.body) != 3
            or message.body[0] != BLUEZ
        ):
            return
        _, previous, current = message.body
        if previous:
            self._registered.clear()
            for address in list(self._sockets):
                self._close_connection(address)
        if current:
            self._schedule_recovery()

    def _schedule_recovery(self) -> None:
        if not self._stopping and self._bus is not None and (
            self._recovery_task is None or self._recovery_task.done()
        ):
            self._recovery_task = asyncio.create_task(self._recover())

    async def _recover(self) -> None:
        """Retry while BlueZ restarts; stop cancels this task on HA unload."""
        delay = 1
        while not self._stopping:
            try:
                await self._register_profiles()
                _LOGGER.info("WBS06 RFCOMM profiles restored after BlueZ restart")
                return
            except Exception:
                _LOGGER.warning("WBS06 RFCOMM re-registration failed; retrying", exc_info=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    def _close_connection(self, address: str) -> None:
        if (sock := self._sockets.get(address)) is not None:
            sock.close()
        if (task := self._tasks.get(address)) is not None:
            task.cancel()

    def accept(self, device_path: str, fd: int) -> None:
        """Take ownership of the D-Bus-passed descriptor, or reject and close it."""
        address = _device_address(device_path)
        if self._stopping or address not in self._handlers or address in self._tasks:
            os.close(fd)
            raise DBusError("org.bluez.Error.Rejected", "unrecognized or busy WBS06")
        sock = None
        try:
            sock = socket.socket(fileno=fd)
            sock.setblocking(False)
        except BaseException:
            # socket.socket(fileno=fd) takes ownership once it succeeds.
            if sock is None:
                os.close(fd)
            else:
                sock.close()
            raise
        self._sockets[address] = sock
        self._tasks[address] = asyncio.create_task(self._serve(address, sock, self._handlers[address]))

    async def _serve(self, address: str, sock: socket.socket, handler: ConnectionHandler) -> None:
        try:
            await handler(sock)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("WBS06 incoming RFCOMM session failed")
        finally:
            sock.close()
            if self._sockets.get(address) is sock:
                self._sockets.pop(address, None)
            if self._tasks.get(address) is asyncio.current_task():
                self._tasks.pop(address, None)

    def disconnect(self, device_path: str) -> None:
        if (address := _device_address(device_path)) is not None:
            self._close_connection(address)

    def release(self, path: str) -> None:
        """BlueZ has removed one profile; re-register it unless shutting down."""
        if self._stopping:
            return
        _LOGGER.warning("BlueZ released a WBS06 RFCOMM profile")
        if path in self._registered:
            self._registered.remove(path)
        for address in list(self._sockets):
            self._close_connection(address)
        self._schedule_recovery()

    async def stop(self) -> None:
        """Unregister any profiles successfully created and release the D-Bus bus."""
        self._stopping = True
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            await asyncio.gather(self._recovery_task, return_exceptions=True)
            self._recovery_task = None
        for address in list(self._sockets):
            self._close_connection(address)
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self._bus is not None:
            try:
                self._bus.remove_message_handler(self._bluez_changed)
                for path in reversed(self._registered):
                    try:
                        reply = await self._bus.call(
                            Message(
                                destination=BLUEZ, path=PROFILE_MANAGER_PATH,
                                interface=PROFILE_MANAGER, member="UnregisterProfile",
                                signature="o", body=[path],
                            )
                        )
                        _check(reply, "unregistering WBS06 RFCOMM profile")
                    except Exception:
                        _LOGGER.warning("WBS06 RFCOMM profile cleanup failed", exc_info=True)
                for path, profile in reversed(self._exported):
                    self._bus.unexport(path, profile)
            finally:
                self._bus.disconnect()
                self._bus = None
                self._registered.clear()
                self._exported.clear()
