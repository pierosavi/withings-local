from withings_wpp.advertisement import FD77, Fd77, WithingsUuid, is_withings


def test_withings_uuid():
    w = WithingsUuid.parse("00000020-5749-5448-0010-000000000000")
    assert (w.service, w.model_id, w.tail) == (0x20, 0x10, "000000000000")
    assert WithingsUuid.parse("0000180f-0000-1000-8000-00805f9b34fb") is None


def test_fd77_flags():
    # version 1, model 0x010; flags TLV (type 1, len 1) = installed | sync requested
    fd = Fd77.parse(bytes.fromhex("1010 11 03"))
    assert (fd.version, fd.model_id) == (1, 0x10)
    assert fd.installed and fd.sync_requested and not fd.wake_up
    assert fd.wants_connection


def test_is_withings():
    assert is_withings([], {FD77: b"\x10\x10"})
    assert not is_withings(["0000180f-0000-1000-8000-00805f9b34fb"], {})


def test_newer_withings_service_uuid_has_no_model():
    # 00000000-0000-5749-5448-494e47530000: "WITH" sits in groups 3-4, so group 4 is no model ID.
    assert WithingsUuid.parse("00000000-0000-5749-5448-494e47530000") is None


def test_fd77_body_comp_setup_and_installed():
    setup = Fd77.parse(bytes.fromhex("1012 11 00 26 001122334455"))
    assert (setup.model_id, setup.installed, setup.mac) == (0x12, False, "00:11:22:33:44:55")
    installed = Fd77.parse(bytes.fromhex("1012 11 03 34 01020304"))
    assert installed.wants_connection and installed.mac is None
