"""WBS06 BlueZ profile lifecycle; fake D-Bus and in-process socket pairs."""

import asyncio
import os
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dbus_fast import Message, MessageType
from dbus_fast.errors import DBusError

from custom_components.withings_local.rfcomm import (
    WBS06_RFCOMM_UUIDS,
    Wbs06Profile,
    Wbs06RfcommServer,
)

ADDRESS = "00:11:22:33:44:55"
DEVICE = "/org/bluez/hci0/dev_00_11_22_33_44_55"
FOREIGN = "/org/bluez/hci0/dev_66_77_88_99_AA_BB"


class FakeBus:
    def __init__(self, fail_on_registration: int = 0):
        self.fail_on_registration = fail_on_registration
        self.calls: list[Message] = []
        self.exports: list[tuple[str, Wbs06Profile]] = []
        self.removed: list[str] = []
        self.disconnected = False
        self.handlers = []

    async def connect(self):
        return self

    def export(self, path, profile):
        self.exports.append((path, profile))

    def unexport(self, path, profile):
        self.removed.append(path)

    async def call(self, message: Message):
        self.calls.append(message)
        registrations = sum(m.member == "RegisterProfile" for m in self.calls)
        if message.member == "RegisterProfile" and registrations == self.fail_on_registration:
            return SimpleNamespace(message_type=MessageType.ERROR, error_name="org.bluez.Error.Failed")
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN)

    def disconnect(self):
        self.disconnected = True

    def add_message_handler(self, handler):
        self.handlers.append(handler)

    def remove_message_handler(self, handler):
        self.handlers.remove(handler)

    def signal_bluez(self, before: str, after: str):
        for handler in list(self.handlers):
            handler(Message(
                message_type=MessageType.SIGNAL,
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="NameOwnerChanged",
                signature="sss",
                body=["org.bluez", before, after],
            ))


async def test_registers_only_two_verified_services_and_unloads_cleanly():
    bus = FakeBus()
    server = Wbs06RfcommServer()
    server.add(ADDRESS, lambda sock: asyncio.sleep(0))
    with patch("custom_components.withings_local.rfcomm.MessageBus", return_value=bus) as factory:
        await server.start()
    factory.assert_called_once()
    assert factory.call_args.kwargs["negotiate_unix_fd"] is True
    registrations = [m for m in bus.calls if m.member == "RegisterProfile"]
    assert len(registrations) == 2
    assert [m.body[1] for m in registrations] == list(WBS06_RFCOMM_UUIDS)
    for msg in registrations:
        assert msg.signature == "osa{sv}"
        options = msg.body[2]
        assert options["Role"].value == "server"
        assert options["Channel"].signature == "q"
        assert options["Channel"].value == 0
        assert options["RequireAuthentication"].value is False
        assert options["RequireAuthorization"].value is False
    await server.stop()
    assert [m.member for m in bus.calls[-2:]] == ["UnregisterProfile", "UnregisterProfile"]
    assert len(bus.removed) == 2
    assert bus.disconnected


async def test_registration_failure_rolls_back_the_first_profile():
    bus = FakeBus(fail_on_registration=2)
    server = Wbs06RfcommServer()
    server.add(ADDRESS, lambda sock: asyncio.sleep(0))
    with patch("custom_components.withings_local.rfcomm.MessageBus", return_value=bus):
        with pytest.raises(RuntimeError, match="registering WBS06"):
            await server.start()
    assert sum(m.member == "UnregisterProfile" for m in bus.calls) == 1
    assert len(bus.removed) == 2
    assert bus.disconnected


async def test_foreign_device_rejected_and_received_fd_closed():
    server = Wbs06RfcommServer()
    server.add(ADDRESS, lambda sock: asyncio.sleep(0))
    local, peer = socket.socketpair()
    fd = os.dup(local.fileno())
    try:
        with pytest.raises(DBusError, match="unrecognized"):
            server.accept(FOREIGN, fd)
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        local.close()
        peer.close()


@pytest.mark.enable_socket
async def test_exact_scale_accepted_over_registered_profile():
    received = asyncio.Event()

    async def handle(sock: socket.socket) -> None:
        data = await asyncio.get_running_loop().sock_recv(sock, 16)
        assert data == b"test"
        received.set()

    server = Wbs06RfcommServer()
    server.add(ADDRESS, handle)
    local, peer = socket.socketpair()
    local.setblocking(False)
    peer.setblocking(False)
    try:
        server.accept(DEVICE, os.dup(local.fileno()))
        await asyncio.get_running_loop().sock_sendall(peer, b"test")
        await asyncio.wait_for(received.wait(), 1)
        await asyncio.sleep(0)  # allow _serve to release the active fd
        assert not server._tasks  # noqa: SLF001
        assert not server._sockets  # noqa: SLF001
    finally:
        await server.stop()
        local.close()
        peer.close()


@pytest.mark.enable_socket
async def test_only_one_incoming_connection_allowed_at_once():
    entered = asyncio.Event()

    async def handle(sock: socket.socket) -> None:
        entered.set()
        await asyncio.Event().wait()

    server = Wbs06RfcommServer()
    server.add(ADDRESS, handle)
    local, peer = socket.socketpair()
    try:
        server.accept(DEVICE, os.dup(local.fileno()))
        await asyncio.wait_for(entered.wait(), 1)
        rejected = os.dup(local.fileno())
        with pytest.raises(DBusError, match="busy"):
            server.accept(DEVICE, rejected)
        with pytest.raises(OSError):
            os.fstat(rejected)
    finally:
        await server.stop()
        local.close()
        peer.close()


@pytest.mark.enable_socket
async def test_two_scales_use_one_pair_of_profiles_and_independent_sessions():
    addresses = ["00:11:22:33:44:55", "66:77:88:99:AA:BB"]
    entered = [asyncio.Event(), asyncio.Event()]
    gate = asyncio.Event()

    async def handle(index: int, sock: socket.socket) -> None:
        entered[index].set()
        await gate.wait()

    bus = FakeBus()
    server = Wbs06RfcommServer()
    server.add(addresses[0], lambda sock: handle(0, sock))
    server.add(addresses[1], lambda sock: handle(1, sock))
    sockets = [socket.socketpair(), socket.socketpair()]
    try:
        with patch("custom_components.withings_local.rfcomm.MessageBus", return_value=bus):
            await server.start()
        assert [msg.body[1] for msg in bus.calls if msg.member == "RegisterProfile"] == list(WBS06_RFCOMM_UUIDS)
        server.accept(DEVICE, os.dup(sockets[0][0].fileno()))
        server.accept(FOREIGN, os.dup(sockets[1][0].fileno()))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 1)
        duplicate = os.dup(sockets[0][0].fileno())
        with pytest.raises(DBusError, match="busy"):
            server.accept(DEVICE, duplicate)
        with pytest.raises(OSError):
            os.fstat(duplicate)
        await server.remove(addresses[0])
        assert addresses[1] in server.addresses
        assert not server._tasks.get(addresses[0])
        gate.set()
    finally:
        await server.stop()
        for pair in sockets:
            pair[0].close()
            pair[1].close()


async def test_bluez_owner_return_restores_missing_profiles_without_duplicate_exports():
    bus = FakeBus()
    server = Wbs06RfcommServer()
    server.add(ADDRESS, lambda sock: asyncio.sleep(0))
    with patch("custom_components.withings_local.rfcomm.MessageBus", return_value=bus):
        await server.start()
        initial_exports = list(bus.exports)
        bus.signal_bluez(":1.12", "")
        assert server._registered == []
        bus.signal_bluez("", ":1.13")
        await asyncio.wait_for(server._recovery_task, 1)
    assert len([m for m in bus.calls if m.member == "RegisterProfile"]) == 4
    assert bus.exports == initial_exports
    await server.stop()
    assert not bus.handlers


async def test_profile_release_restores_single_profile():
    bus = FakeBus()
    server = Wbs06RfcommServer()
    server.add(ADDRESS, lambda sock: asyncio.sleep(0))
    with patch("custom_components.withings_local.rfcomm.MessageBus", return_value=bus):
        await server.start()
        first = bus.exports[0][1]
        first.Release()
        await asyncio.wait_for(server._recovery_task, 1)
    registered = [m.body[1] for m in bus.calls if m.member == "RegisterProfile"]
    assert registered == [*WBS06_RFCOMM_UUIDS, WBS06_RFCOMM_UUIDS[0]]
    await server.stop()


async def test_bluez_restart_retries_when_registration_temporarily_fails():
    bus = FakeBus()
    server = Wbs06RfcommServer()
    server.add(ADDRESS, lambda sock: asyncio.sleep(0))
    try:
        with patch("custom_components.withings_local.rfcomm.MessageBus", return_value=bus):
            await server.start()
            bus.fail_on_registration = 3
            bus.signal_bluez(":1.20", "")
            bus.signal_bluez("", ":1.21")
            await asyncio.sleep(0)
            assert len(server._registered) == 0
            bus.fail_on_registration = 0
            await asyncio.wait_for(server._recovery_task, 2.5)
        assert len(server._registered) == 2
        assert [m.body[1] for m in bus.calls if m.member == "RegisterProfile"][-2:] == list(WBS06_RFCOMM_UUIDS)
    finally:
        await server.stop()


async def test_unload_prevents_profile_recovery():
    bus = FakeBus()
    server = Wbs06RfcommServer()
    server.add(ADDRESS, lambda sock: asyncio.sleep(0))
    with patch("custom_components.withings_local.rfcomm.MessageBus", return_value=bus):
        await server.start()
    profile = bus.exports[0][1]
    await server.stop()
    count = sum(m.member == "RegisterProfile" for m in bus.calls)
    profile.Release()
    bus.signal_bluez(":1.21", ":1.22")
    await asyncio.sleep(0)
    assert sum(m.member == "RegisterProfile" for m in bus.calls) == count
    assert server._recovery_task is None
