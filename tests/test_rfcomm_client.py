"""RFCOMM WPP transport tests use an in-memory stream, not Bluetooth hardware."""

import asyncio
import socket
import struct

import pytest

from withings_wpp import auth
from withings_wpp import client as wpp_client
from withings_wpp.client import RfcommWppSession, ScaleClient, ScaleDisconnected, read_measurements
from withings_wpp.codec import Message, Tlv, decode_message, pack_str
from withings_wpp.fragfsm import FragFsmReassembler, frame
from withings_wpp.messages import Cmd, Obj

MAC = "00:11:22:33:44:55"  # synthetic, never a real device identifier
KL = "test-kl-not-a-real-secret-000000"
CHALLENGE = bytes(range(16))


@pytest.fixture(autouse=True)
def disable_real_hardware_settle_delay(monkeypatch) -> None:
    monkeypatch.setattr(wpp_client, "FRAGFSM_SETTLE_SECONDS", 0)


async def test_rfcomm_accepts_fragmented_stream_reply() -> None:
    """The stream transport reuses the GATT WPP decoder and request matching."""
    local, peer = socket.socketpair()
    local.setblocking(False)
    peer.setblocking(False)
    session = RfcommWppSession(local)
    loop = asyncio.get_running_loop()
    try:
        await loop.sock_sendall(peer, b"\x01\x00")
        await session.start()
        request = asyncio.create_task(ScaleClient(session).battery())
        data = await asyncio.wait_for(loop.sock_recv(peer, 128), 1)
        assert decode_message(FragFsmReassembler().feed(data)[0]).command == Cmd.BATTERY_PERCENT
        reply = Message(Cmd.BATTERY_PERCENT, (Tlv(Obj.BATTERY_PERCENT, struct.pack(">H", 81)),)).encode()
        wrapped = frame(reply)
        await loop.sock_sendall(peer, wrapped[:2])
        await loop.sock_sendall(peer, wrapped[2:6])
        await loop.sock_sendall(peer, wrapped[6:])
        assert await asyncio.wait_for(request, 1) == 81
        assert session.sync_ok is False
    finally:
        await session.close()
        peer.close()


async def test_rfcomm_disconnect_releases_waiting_request() -> None:
    local, peer = socket.socketpair()
    local.setblocking(False)
    peer.setblocking(False)
    session = RfcommWppSession(local)
    try:
        await asyncio.get_running_loop().sock_sendall(peer, b"\x01\x00")
        await session.start()
        request = asyncio.create_task(ScaleClient(session).battery())
        data = await asyncio.wait_for(asyncio.get_running_loop().sock_recv(peer, 128), 1)
        assert decode_message(FragFsmReassembler().feed(data)[0]).command == Cmd.BATTERY_PERCENT
        peer.close()
        with pytest.raises(ScaleDisconnected, match="disconnected"):
            await asyncio.wait_for(request, 1)
    finally:
        await session.close()
        peer.close()


async def test_rfcomm_session_refuses_duplicate_reader() -> None:
    local, peer = socket.socketpair()
    local.setblocking(False)
    peer.setblocking(False)
    session = RfcommWppSession(local)
    try:
        await asyncio.get_running_loop().sock_sendall(peer, b"\x01\x00")
        await session.start()
        with pytest.raises(RuntimeError, match="already started"):
            await session.start()
    finally:
        await session.close()
        peer.close()


async def test_rfcomm_waits_for_fragmented_reset_before_wpp() -> None:
    local, peer = socket.socketpair()
    local.setblocking(False)
    peer.setblocking(False)
    session = RfcommWppSession(local)
    loop = asyncio.get_running_loop()
    try:
        starting = asyncio.create_task(session.start())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(loop.sock_recv(peer, 64), 0.02)
        assert not starting.done()
        await loop.sock_sendall(peer, b"\x01")
        assert not starting.done()
        await loop.sock_sendall(peer, b"\x00")
        await asyncio.wait_for(starting, 1)
        request = asyncio.create_task(ScaleClient(session).battery())
        data = await asyncio.wait_for(loop.sock_recv(peer, 64), 1)
        assert decode_message(FragFsmReassembler().feed(data)[0]).command == Cmd.BATTERY_PERCENT
        reply = Message(Cmd.BATTERY_PERCENT, (Tlv(Obj.BATTERY_PERCENT, struct.pack(">H", 87)),))
        await loop.sock_sendall(peer, frame(reply.encode()))
        assert await asyncio.wait_for(request, 1) == 87
    finally:
        await session.close()
        peer.close()


@pytest.mark.parametrize("knows_key", [True, False])
async def test_rfcomm_uses_verified_wpp_authentication_before_reading(knows_key: bool) -> None:
    """An accepted RFCOMM socket never makes measurements trusted by itself."""
    local, peer = socket.socketpair()
    local.setblocking(False)
    peer.setblocking(False)
    commands: list[int] = []

    async def scale() -> None:
        reassembler = FragFsmReassembler()
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(peer, b"\x01\x00")
        while chunk := await loop.sock_recv(peer, 4096):
            for raw in reassembler.feed(chunk):
                assert raw is not None
                req = decode_message(raw)
                commands.append(req.command)
                reply: Message | None = None
                if req.command == Cmd.PROBE:
                    assert req.find(Obj.APP_PROBE)[0].value == struct.pack(">BBI", 1, 1, 8050004)
                    assert req.find(Obj.APP_PROBE_OS_VERSION)
                    reply = Message(Cmd.PROBE_CHALLENGE, (
                        Tlv(Obj.PROBE_CHALLENGE, pack_str(MAC) + pack_str(CHALLENGE)),
                    ))
                elif req.command == Cmd.PROBE_CHALLENGE:
                    theirs = req.find(Obj.PROBE_CHALLENGE)[0].value[len(MAC) + 2 :]
                    answer = auth.answer(theirs, MAC, KL if knows_key else "wrong-key")
                    reply = Message(Cmd.PROBE, (Tlv(Obj.PROBE_CHALLENGE_RESPONSE, pack_str(answer)),))
                elif req.command == Cmd.BATTERY_PERCENT:
                    reply = Message(Cmd.BATTERY_PERCENT, (
                        Tlv(Obj.BATTERY_PERCENT, struct.pack(">H", 79)),
                    ))
                elif req.command == Cmd.CONNECT_REASON:
                    reply = Message(Cmd.CONNECT_REASON, (
                        Tlv(Obj.CONNECT_REASON, struct.pack(">H", 2)),
                    ))
                elif req.command == Cmd.STORED_MEASURE:
                    assert req.find(Obj.STORED_MEASURE_ACTION)
                    reply = Message(Cmd.STORED_MEASURE, (
                        Tlv(Obj.STORED_MEASURE_STATUS, struct.pack(">hib", 0, 0, 0)),
                    ))
                elif req.command == Cmd.DISCONNECT:
                    return
                else:
                    pytest.fail(f"unexpected WPP operation {req.command}")
                if reply is not None:
                    await loop.sock_sendall(peer, frame(reply.encode()))

    task = asyncio.create_task(scale())
    session = RfcommWppSession(local, sync_ok=False)
    try:
        result = await asyncio.wait_for(
            read_measurements(None, KL, session, app_version=8050004, authentication_timeout=1.5), 2
        )
        assert result.authenticated
        assert result.scale_verified is knows_key
        assert result.battery == (79 if knows_key else None)
        await asyncio.wait_for(task, 1)
        assert Cmd.SYNC_OK not in commands
        assert Cmd.DISCONNECT in commands
        assert session.rx_bytes > 0
        assert session.tx_bytes > 0
        assert session.rx_chunks > 0
        if not knows_key:
            assert Cmd.STORED_MEASURE not in commands
            assert Cmd.BATTERY_PERCENT not in commands
    finally:
        await session.close()
        peer.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
