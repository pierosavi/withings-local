"""Legacy Withings Classic Bluetooth packet framing.

WBS06 wraps WPP messages inside FragFsm, unlike the newer GATT
characteristic. A two-byte RESET (01 00) starts the conversation.
DATA (01 01) carries a big-endian uint16 length and raw WPP bytes.
"""

from dataclasses import dataclass, field
import struct

from .codec import WppError

VERSION = 1
RESET = 0
DATA = 1
MAX_PAYLOAD = 0xFFFF
DATA_HEADER = struct.Struct(">BBH")


def frame(payload: bytes) -> bytes:
    """Wrap exactly one raw WPP packet for an RFCOMM byte stream."""
    if not 0 < len(payload) <= MAX_PAYLOAD:
        raise WppError("invalid FragFsm payload size")
    return DATA_HEADER.pack(VERSION, DATA, len(payload)) + payload


@dataclass
class FragFsmReassembler:
    """Emit one event per complete frame; None represents a RESET."""

    _buffer: bytearray = field(default_factory=bytearray)

    def feed(self, chunk: bytes) -> list[bytes | None]:
        self._buffer.extend(chunk)
        events: list[bytes | None] = []
        while len(self._buffer) >= 2:
            version, operation = self._buffer[:2]
            if version != VERSION:
                raise WppError(f"unexpected FragFsm protocol version {version}")
            if operation == RESET:
                del self._buffer[:2]
                events.append(None)
                continue
            if operation != DATA:
                raise WppError(f"unexpected FragFsm command {operation}")
            if len(self._buffer) < DATA_HEADER.size:
                break
            length = struct.unpack_from(">H", self._buffer, 2)[0]
            if length == 0:
                raise WppError("empty FragFsm data frame")
            if len(self._buffer) < DATA_HEADER.size + length:
                break
            events.append(bytes(self._buffer[DATA_HEADER.size : DATA_HEADER.size + length]))
            del self._buffer[: DATA_HEADER.size + length]
        return events
