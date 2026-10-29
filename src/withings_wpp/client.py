"""Talk WPP to a Withings scale over a connected bleak client.

Only read operations are exposed. Commands that change the scale (setup, association keys,
units, Wi-Fi, firmware, deleting measurements) are deliberately not implemented here.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

from . import auth
from .codec import DEVICE_REQUEST, Message, Reassembler, Tlv, WppError
from .fragfsm import FragFsmReassembler, frame as fragfsm_frame
from .measures import MeasureGroup, merge_by_time, parse_groups
from .messages import (
    APP_VERSION,
    Cmd,
    CmdError,
    MeasureAction,
    Obj,
    ProbeChallenge,
    ProbeReply,
    StoredMeasureStatus,
    battery_request,
    connect_reason_request,
    describe,
    probe,
    stored_measure,
)

_LOGGER = logging.getLogger(__name__)
WITH_MARKER = "-5749-5448-"
CHAR_PREFIX = "00000024"
ERR_UNSUPPORTED = -3
SUBSCRIBE_TIMEOUT = 15.0
WRITE_TIMEOUT = 10.0
FRAGFSM_RESET_TIMEOUT = 10.0
FRAGFSM_SETTLE_SECONDS = 0.5


class WppCommandError(Exception):
    def __init__(self, error: CmdError) -> None:
        super().__init__(str(error))
        self.error = error


class AuthenticationError(Exception):
    pass


class ScaleDisconnected(Exception):
    """The scale dropped the Bluetooth link mid-session."""


def find_wpp_characteristic(client: BleakClient) -> BleakGATTCharacteristic:
    for service in client.services:
        for char in service.characteristics:
            if char.uuid.startswith(CHAR_PREFIX) and WITH_MARKER in char.uuid:
                return char
    raise LookupError("Withings WPP characteristic not found")


@dataclass
class Authenticated:
    reply: ProbeReply | None
    authenticated: bool
    scale_verified: bool


class WppConversation:
    """Shared WPP request/response handling over GATT or an RFCOMM byte stream.

    `sync_ok` controls both SYNC_OK paths: sending one at the end of a read, and echoing one the
    device sends as a request. Devices whose reaction to it is unknown should turn it off.
    """

    def __init__(self, trace=None, *, sync_ok: bool = True) -> None:
        self._reasm = Reassembler()
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()
        self._trace = trace or (lambda direction, raw: None)
        self._disconnected = asyncio.Event()
        self.sync_ok = sync_ok

    def mark_disconnected(self) -> None:
        """Call from the client's disconnect callback: pending operations then fail at once."""
        self._disconnected.set()

    async def _until_disconnect(self, aw, what: str):
        """Await `aw`, but raise as soon as the scale drops the link (some Bluetooth stacks
        leave the pending call hanging instead of failing it)."""
        task = asyncio.ensure_future(aw)
        gone = asyncio.ensure_future(self._disconnected.wait())
        try:
            done, _ = await asyncio.wait({task, gone}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            raise ScaleDisconnected(f"the scale disconnected while {what}")
        finally:
            for t in (task, gone):
                if not t.done():
                    t.cancel()

    def _on_notify(self, _char, data: bytearray) -> None:
        self._trace("rx", bytes(data))
        for msg in self._reasm.feed(bytes(data)):
            _LOGGER.debug("<- %s", describe(msg))
            if msg.from_device and msg.base_command != Cmd.ERROR:
                asyncio.get_running_loop().create_task(self._answer_device_request(msg))
            else:
                self._inbox.put_nowait(msg)

    async def _answer_device_request(self, msg: Message) -> None:
        if msg.base_command == Cmd.SYNC_OK and self.sync_ok:
            # The scale expects its sync acknowledgement echoed back.
            await self.send(Message(Cmd.SYNC_OK | DEVICE_REQUEST))
            return
        # Like the phone app does for requests it doesn't handle: reply "unsupported".
        err = Tlv(Obj.CMD_ERROR, struct.pack(">Hi", msg.base_command, ERR_UNSUPPORTED))
        await self.send(Message(Cmd.ERROR | DEVICE_REQUEST, (err,)))

    async def send(self, msg: Message) -> None:
        raw = msg.encode()
        _LOGGER.debug("-> %s", describe(msg))
        self._trace("tx", raw)
        async with asyncio.timeout(WRITE_TIMEOUT):
            await self._until_disconnect(self._write(raw), "writing")

    async def _write(self, raw: bytes) -> None:
        raise NotImplementedError

    async def request(
        self, msg: Message, *, expect: set[int] | None = None, multi: bool = False, timeout: float = 5.0
    ) -> list[Message]:
        """Send and collect the reply. Multi-message replies end with a Null TLV.

        Only messages whose command is in `expect` (default: the command sent) count as the reply.
        Anything else is something the device sent on its own, such as the messages some devices
        push right after the subscription; it is logged and dropped, as are messages left over from
        before this request. Errors always count: they are raised whichever command they name.
        """
        expect = expect or {msg.base_command}
        self._drop_stale()
        await self.send(msg)
        replies: list[Message] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                if replies:
                    return replies
                raise TimeoutError(f"no reply to command {msg.command}")
            async with asyncio.timeout(remaining):
                reply = await self._until_disconnect(self._inbox.get(), "waiting for a reply")
            if reply.base_command == Cmd.ERROR:
                (tlv,) = reply.find(Obj.CMD_ERROR) or (None,)
                if tlv is not None:
                    raise WppCommandError(CmdError.parse(tlv.value))
                raise WppCommandError(CmdError(msg.command, 0))
            if reply.base_command not in expect:
                _LOGGER.debug("ignoring unsolicited %s while waiting for %s", describe(reply), msg.command)
                continue
            replies.append(reply)
            if not multi or reply.find(Obj.NULL):
                return replies

    def _drop_stale(self) -> None:
        while not self._inbox.empty():
            _LOGGER.debug("dropping unanswered %s", describe(self._inbox.get_nowait()))


class WppSession(WppConversation):
    """WPP conversation over the scale's notifying GATT characteristic."""

    def __init__(self, client: BleakClient, trace=None, *, sync_ok: bool = True) -> None:
        super().__init__(trace, sync_ok=sync_ok)
        self._client = client
        self._char = find_wpp_characteristic(client)

    async def start(self) -> None:
        _LOGGER.debug("subscribing to notifications on %s", self._char.uuid)
        try:
            async with asyncio.timeout(SUBSCRIBE_TIMEOUT):
                await self._until_disconnect(self._client.start_notify(self._char, self._on_notify), "subscribing")
        except TimeoutError as err:
            raise TimeoutError(
                f"subscribing to notifications took more than {SUBSCRIBE_TIMEOUT:.0f}s "
                "(the Bluetooth stack may be trying to pair with the scale)"
            ) from err
        _LOGGER.debug("subscribed")

    async def _write(self, raw: bytes) -> None:
        await self._client.write_gatt_char(self._char, raw, response=True)


class RfcommWppSession(WppConversation):
    """WPP over FragFsm-framed Classic Bluetooth RFCOMM.

    The accepted socket is owned by the caller. No Classic pairing is
    initiated here; the request/response protocol still verifies the
    scale's application-level key before accepting measurements.
    """

    def __init__(self, sock: socket.socket, trace=None, *, sync_ok: bool = False) -> None:
        super().__init__(trace, sync_ok=sync_ok)
        self._sock = sock
        self._reader_task: asyncio.Task | None = None
        self._fragfsm = FragFsmReassembler()
        self._reset_received = asyncio.Event()
        # Aggregate transport diagnostics only. Never persist frame bytes,
        # challenges, measurement values, Bluetooth addresses, or the KL.
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.rx_chunks = 0

    async def start(self) -> None:
        if self._reader_task is not None:
            raise RuntimeError("RFCOMM reader already started")
        self._reader_task = asyncio.create_task(self._receive())
        # Wait for the scale's RESET before starting WPP.
        try:
            async with asyncio.timeout(FRAGFSM_RESET_TIMEOUT):
                await self._until_disconnect(self._reset_received.wait(), "waiting for FragFsm RESET")
        except TimeoutError:
            _LOGGER.warning("WBS06 did not send FragFsm RESET before WPP initialization")
        else:
            await asyncio.sleep(FRAGFSM_SETTLE_SECONDS)

    async def _receive(self) -> None:
        try:
            while chunk := await asyncio.get_running_loop().sock_recv(self._sock, 4096):
                self.rx_bytes += len(chunk)
                self.rx_chunks += 1
                for payload in self._fragfsm.feed(chunk):
                    if payload is None:
                        self._reset_received.set()
                        _LOGGER.debug("WBS06 FragFsm RESET received")
                    else:
                        self._on_notify(None, bytearray(payload))
        except (OSError, WppError):
            _LOGGER.debug("RFCOMM stream ended unexpectedly", exc_info=True)
        finally:
            self.mark_disconnected()

    async def _write(self, raw: bytes) -> None:
        packet = fragfsm_frame(raw)
        await asyncio.get_running_loop().sock_sendall(self._sock, packet)
        self.tx_bytes += len(packet)

    async def close(self) -> None:
        self.mark_disconnected()
        self._sock.close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)


class ScaleClient:
    """High-level read-only scale operations."""

    def __init__(self, session: WppConversation) -> None:
        self.session = session

    async def authenticate(
        self, kl: str, *, app_version: int = APP_VERSION, timeout: float = 5.0
    ) -> Authenticated:
        # The device answers a PROBE with PROBE_CHALLENGE, or with PROBE when it skips authentication.
        answers = {Cmd.PROBE, Cmd.PROBE_CHALLENGE}
        (reply,) = await self.session.request(probe(app_version=app_version), expect=answers, timeout=timeout)
        if reply.base_command == Cmd.PROBE:
            # Setup/button session: the scale didn't ask for authentication.
            return Authenticated(_probe_reply(reply), False, False)
        tlvs = reply.find(Obj.PROBE_CHALLENGE)
        if not tlvs:
            raise AuthenticationError(f"unexpected probe answer: {describe(reply)}")
        challenge = ProbeChallenge.parse(tlvs[0].value)
        own = auth.new_challenge()
        (final,) = await self.session.request(
            auth.challenge_reply(challenge.challenge, challenge.mac, kl, own), expect=answers, timeout=timeout
        )
        their_answer = final.find(Obj.PROBE_CHALLENGE_RESPONSE)
        verified = bool(their_answer) and auth.verify(their_answer[0].value[1:], own, challenge.mac, kl)
        return Authenticated(_probe_reply(final), True, verified)

    async def connect_reason(self) -> int:
        (reply,) = await self.session.request(connect_reason_request())
        (tlv,) = reply.find(Obj.CONNECT_REASON)
        return int.from_bytes(tlv.value[:2], "big")

    async def battery(self) -> int:
        """Battery level in percent (a read; the phone app asks for it on every sync)."""
        (reply,) = await self.session.request(battery_request())
        (tlv,) = reply.find(Obj.BATTERY_PERCENT)
        return int.from_bytes(tlv.value[:2], "big")

    async def measure_status(self) -> StoredMeasureStatus:
        (reply,) = await self.session.request(stored_measure(MeasureAction.GET_STATE))
        (tlv,) = reply.find(Obj.STORED_MEASURE_STATUS)
        return StoredMeasureStatus.parse(tlv.value)

    async def measures(self) -> list[MeasureGroup]:
        replies = await self.session.request(stored_measure(MeasureAction.GET_ALL), multi=True, timeout=10)
        return parse_groups(replies)

    async def sync_ok(self) -> None:
        """Acknowledge the sync. Reported as required: without it the scale may ignore the next
        connection. Reported not to delete or mark readings, so the phone app still gets them."""
        await self.session.request(Message(Cmd.SYNC_OK), timeout=3)

    async def disconnect(self) -> None:
        # The phone app sends this without waiting for an answer.
        await self.session.send(Message(Cmd.DISCONNECT))


def _probe_reply(msg: Message) -> ProbeReply | None:
    """The device description in a PROBE answer. It only tells the firmware version, so a layout we
    can't parse must not fail the session."""
    tlvs = msg.find(Obj.PROBE_REPLY)
    if not tlvs:
        return None
    try:
        return ProbeReply.parse(tlvs[0].value)
    except WppError:
        _LOGGER.debug("unparsable PROBE_REPLY (%d bytes)", len(tlvs[0].value), exc_info=True)
        return None


@dataclass
class SyncResult:
    authenticated: bool
    scale_verified: bool
    connect_reason: int | None
    firmware: int | None
    groups: list[MeasureGroup]
    battery: int | None = None


async def read_measurements(
    ble: BleakClient | None,
    kl: str,
    session: WppConversation | None = None,
    *,
    app_version: int = APP_VERSION,
    authentication_timeout: float = 5.0,
) -> SyncResult:
    """Run one read-only session on a connected client: authenticate, read, disconnect.

    Pass a pre-built `session` to wire its `mark_disconnected` into the client's disconnect callback.
    """
    if session is None:
        if ble is None:
            raise ValueError("a BLE client or a connected WPP session is required")
        session = WppSession(ble)
    await session.start()
    scale = ScaleClient(session)
    auth_result = await scale.authenticate(
        kl, app_version=app_version, timeout=authentication_timeout
    )
    firmware = auth_result.reply.soft_version if auth_result.reply else None
    if not auth_result.authenticated:
        return SyncResult(False, False, None, firmware, [])
    if not auth_result.scale_verified:
        # A device that can't prove it knows kl gets no further commands: nothing is read from it
        # and no SYNC_OK is sent to it.
        await _say_goodbye(scale)
        return SyncResult(True, False, None, firmware, [])
    try:
        battery = await scale.battery()
    except (WppCommandError, TimeoutError, ValueError):
        _LOGGER.debug("battery level not available", exc_info=True)
        battery = None
    try:
        reason = await scale.connect_reason()
    except (WppCommandError, TimeoutError, ValueError):
        _LOGGER.debug("connect reason not available", exc_info=True)
        reason = None
    status = await scale.measure_status()
    groups = merge_by_time(await scale.measures()) if status.count > 0 else []
    if session.sync_ok:
        try:
            await scale.sync_ok()
        except (WppCommandError, TimeoutError):
            _LOGGER.debug("SYNC_OK not acknowledged", exc_info=True)
    await _say_goodbye(scale)
    return SyncResult(True, True, reason, firmware, groups, battery)


async def _say_goodbye(scale: ScaleClient) -> None:
    try:
        await scale.disconnect()
    except Exception:  # noqa: BLE001 - the scale may already be gone
        _LOGGER.debug("disconnect message not delivered", exc_info=True)
