import struct

from withings_wpp.auth import answer, challenge_reply, verify
from withings_wpp.codec import Message, Tlv, decode_message
from withings_wpp.measures import parse_groups
from withings_wpp.messages import Cmd, Obj

# Synthetic test values only.
MAC = "00:11:22:33:44:55"
KL = "test-kl-not-a-real-secret-000000"


def test_answer_matches_documented_sha1_vector():
    ch = bytes(range(16))
    # Fixed SHA1 fixture for 00..0f || ASCII MAC || ASCII kl, not derived from answer().
    assert answer(ch, MAC, KL).hex() == "9436bcff5e90ec1b0a13cd1776e98e1095ff4f53"


def test_challenge_reply_layout_and_verify():
    own = b"\xaa" * 16
    msg = decode_message(challenge_reply(bytes(16), MAC, KL, own).encode())
    assert msg.command == Cmd.PROBE_CHALLENGE
    resp, chal = msg.tlvs
    assert resp.type == Obj.PROBE_CHALLENGE_RESPONSE and resp.value[0] == 20
    assert chal.type == Obj.PROBE_CHALLENGE and chal.value == bytes([17]) + MAC.encode() + bytes([16]) + own
    assert verify(answer(own, MAC, KL), own, MAC, KL)
    assert not verify(b"\x00" * 20, own, MAC, KL)


def _meta(uid, valid, ids, attrib, t):
    return (
        struct.pack(">IBB", uid, valid, len(ids))
        + b"".join(struct.pack(">I", u) for u in ids)
        + struct.pack(">BI", attrib, t)
    )


def _data(raw, mtype, exp):
    return struct.pack(">iHh", raw, mtype, exp)


def test_parse_groups_trims_junk_user_ids():
    # Real layout: 1 valid ID in a 3-slot array (slots 2 and 3 are junk).
    msgs = [
        Message(Cmd.STORED_MEASURE, (Tlv(Obj.STORED_MEASURE_META, _meta(7, 1, [42, 0xDEAD, 0xBEEF], 0, 1_700_000_000)),
                                     Tlv(Obj.STORED_MEASURE_DATA, _data(72345, 1, -3)),
                                     Tlv(Obj.STORED_MEASURE_DATA, _data(215, 6, -1)))),
        Message(Cmd.STORED_MEASURE, (Tlv(Obj.NULL, b""),)),
    ]
    (g,) = parse_groups(msgs)
    assert g.user_ids == [42] and g.time.timestamp() == 1_700_000_000
    assert g.value("weight") == 72.345 and g.value("fat_ratio") == 21.5


def test_meta_with_trailing_bytes_is_rejected():
    import pytest

    from withings_wpp.codec import WppError
    from withings_wpp.measures import parse_meta

    with pytest.raises(WppError):
        parse_meta(_meta(7, 1, [42], 0, 1) + b"\x00")


def test_merge_by_time_dedupes_on_time_and_type():
    from datetime import UTC, datetime

    from withings_wpp.measures import Measure, MeasureGroup, merge_by_time

    t1, t2 = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    groups = [
        MeasureGroup(t2, [], 0, 0, [Measure(1, 70.0)]),
        MeasureGroup(t1, [], 0, 0, [Measure(1, 71.0)]),
        MeasureGroup(t2, [], 0, 0, [Measure(11, 60.0)]),
        MeasureGroup(t2, [], 0, 0, [Measure(1, 70.0)]),
    ]
    merged = merge_by_time(groups)
    assert [g.time for g in merged] == [t1, t2]
    assert sorted((m.type, m.value) for m in merged[1].measures) == [(1, 70.0), (11, 60.0)]


def test_bad_meta_drops_its_values_instead_of_moving_them():
    msgs = [
        Message(Cmd.STORED_MEASURE, (Tlv(Obj.STORED_MEASURE_META, _meta(7, 1, [42, 0, 0], 0, 1_700_000_000)),
                                     Tlv(Obj.STORED_MEASURE_DATA, _data(72345, 1, -3)),
                                     Tlv(Obj.STORED_MEASURE_META, _meta(8, 1, [43], 0, 1_700_000_100) + b"\x00"),
                                     Tlv(Obj.STORED_MEASURE_DATA, _data(80000, 1, -3)),
                                     Tlv(Obj.STORED_MEASURE_META, _meta(9, 1, [44], 0, 1_700_000_200)),
                                     Tlv(Obj.STORED_MEASURE_DATA, b"\x00"),
                                     Tlv(Obj.STORED_MEASURE_DATA, _data(60000, 1, -3)),
                                     Tlv(Obj.NULL, b""))),
    ]
    first, last = parse_groups(msgs)
    assert first.user_ids == [42] and first.value("weight") == 72.345
    assert last.user_ids == [44] and last.value("weight") == 60.0


def test_data_before_any_meta_is_dropped():
    msgs = [Message(Cmd.STORED_MEASURE, (Tlv(Obj.STORED_MEASURE_DATA, _data(72345, 1, -3)), Tlv(Obj.NULL, b"")))]
    assert parse_groups(msgs) == []
