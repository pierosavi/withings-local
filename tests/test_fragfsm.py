"""FragFsm tests for the WBS06 Classic wire format."""

import pytest

from withings_wpp.codec import WppError
from withings_wpp.fragfsm import FragFsmReassembler, frame


def test_fragfsm_data_frame_has_exact_header() -> None:
    # Version 1, DATA 1, 16-bit big-endian length 5, then a WPP header.
    assert frame(b"\x01\x01\x01\x00\x00") == bytes.fromhex(
        "01 01 00 05 01 01 01 00 00"
    )


def test_known_reset_and_data_wire_bytes_decode_without_encoder() -> None:
    # Two-byte RESET, then DATA + length 5 + one WPP header, as seen on WBS06.
    assert FragFsmReassembler().feed(bytes.fromhex(
        "01 00 01 01 00 05 01 01 01 00 00"
    )) == [None, bytes.fromhex("01 01 01 00 00")]


def test_two_byte_reset_and_fragmented_reset() -> None:
    decoder = FragFsmReassembler()
    assert decoder.feed(b"\x01") == []
    assert decoder.feed(b"\x00") == [None]
    assert decoder.feed(b"\x01\x00\x01\x00") == [None, None]


def test_fragmented_and_coalesced_frames() -> None:
    decoder = FragFsmReassembler()
    first = frame(b"abcde")
    second = frame(b"z")
    assert decoder.feed(b"\x01\x00" + first[:3]) == [None]
    assert decoder.feed(first[3:6]) == []
    assert decoder.feed(first[6:] + second + first) == [
        b"abcde", b"z", b"abcde"
    ]


@pytest.mark.parametrize("packet", [
    b"\x00\x00",
    b"\x01\x02",
    b"\x01\x01\x00\x00",
])
def test_reject_invalid_control_or_empty_frame(packet: bytes) -> None:
    with pytest.raises(WppError):
        FragFsmReassembler().feed(packet)


@pytest.mark.parametrize("length", [0, 65536])
def test_reject_invalid_outgoing_payload_size(length: int) -> None:
    with pytest.raises(WppError):
        frame(b"x" * length)
