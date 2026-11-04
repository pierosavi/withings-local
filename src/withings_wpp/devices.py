"""What differs between Withings devices that speak WPP over Bluetooth Low Energy.

The codec, authentication and stored-measurement commands are the same on every device; a
profile only records the differences. Body (WBS06) isn't here: it speaks WPP over Classic
RFCOMM, not GATT.

The Body Comp profile comes from a community report (issue #5, one WBS12 on a Raspberry Pi with
BlueZ) and hasn't been tested with Home Assistant yet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .advertisement import FD77, WITH_MARKER, Fd77, WithingsUuid

# Withings' newer GATT service; the tail is ASCII "WITHINGS". WPP runs on characteristic …0004.
WITHINGS_SERVICE = "00000000-0000-5749-5448-494e47530000"
WITHINGS_SERVICE_WPP = "00000004-0000-5749-5448-494e47530000"


@dataclass(frozen=True)
class DeviceProfile:
    model_id: int
    name: str
    # Model name in the device registry.
    model_code: str
    # Matches the WPP characteristic UUID (lower case).
    is_wpp_characteristic: Callable[[str], bool]
    # Send SYNC_OK after a read and echo the device's own. Off where its effect is unknown.
    sync_ok: bool


BODY_SMART = DeviceProfile(
    model_id=0x0010,
    name="Body Smart",
    model_code="WBS13",
    is_wpp_characteristic=lambda uuid: uuid.startswith("00000024") and WITH_MARKER in uuid,
    sync_ok=True,
)

BODY_COMP = DeviceProfile(
    model_id=0x0012,
    name="Body Comp",
    model_code="WBS12",
    # Reported: `…0005` is also writable and notifies, but PROBE was only tried on `…0004`.
    is_wpp_characteristic=lambda uuid: uuid == WITHINGS_SERVICE_WPP,
    # SYNC_OK's effect on this model is unknown: never send it.
    sync_ok=False,
)

PROFILES = {p.model_id: p for p in (BODY_SMART, BODY_COMP)}


def model_ids(service_uuids, service_data: dict) -> set[int]:
    """Withings model IDs in an advertisement: from the "WITH" service UUID and the FD77 data."""
    ids = {w.model_id for u in service_uuids if (w := WithingsUuid.parse(u))}
    for key, value in service_data.items():
        if key.lower() == FD77 and (fd := Fd77.parse(bytes(value))):
            ids.add(fd.model_id)
    return ids


def profile_for(service_uuids, service_data: dict, name: str | None = None) -> DeviceProfile | None:
    """The supported BLE device this advertisement comes from, if any."""
    if name:
        for profile in PROFILES.values():
            if name.startswith(profile.name):
                return profile
    return next((PROFILES[m] for m in sorted(model_ids(service_uuids, service_data)) if m in PROFILES), None)
