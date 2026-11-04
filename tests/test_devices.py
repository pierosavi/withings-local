from withings_wpp.advertisement import FD77
from withings_wpp.devices import BODY_COMP, BODY_SMART, profile_for

NEW_WPP = "00000004-0000-5749-5448-494e47530000"
NEW_OTHER = "00000005-0000-5749-5448-494e47530000"
OLD_WPP = "00000024-5749-5448-0010-000000000000"


def test_profile_from_fd77_uuid_or_name():
    assert profile_for([], {FD77: bytes.fromhex("1012 11 03 34 01020304")}) is BODY_COMP
    assert profile_for(["00000020-5749-5448-0010-001122334455"], {}) is BODY_SMART
    assert profile_for([], {}, "Body Comp 55") is BODY_COMP
    assert profile_for([], {}, "Body Smart 55") is BODY_SMART
    assert profile_for([], {FD77: bytes.fromhex("1047 11 01 34")}) is None  # BeamO: not supported here


def test_wpp_characteristic_per_profile():
    assert BODY_COMP.is_wpp_characteristic(NEW_WPP)
    assert not BODY_COMP.is_wpp_characteristic(NEW_OTHER)
    assert not BODY_COMP.is_wpp_characteristic(OLD_WPP)
    assert BODY_SMART.is_wpp_characteristic(OLD_WPP)
    assert not BODY_SMART.is_wpp_characteristic(NEW_WPP)


def test_sync_ok_only_where_known():
    assert BODY_SMART.sync_ok and not BODY_COMP.sync_ok
