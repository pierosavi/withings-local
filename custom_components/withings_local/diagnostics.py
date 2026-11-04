"""Diagnostics download: what the integration last saw, with the key and addresses removed."""

from __future__ import annotations

from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .const import CONF_KL, CONF_USERS
from .coordinator import WITHINGS_COMPANY_ID, WithingsLocalConfigEntry, has_new_data
from .withings_wpp.advertisement import FD77

TO_REDACT = {CONF_KL, CONF_ADDRESS, CONF_USERS}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: WithingsLocalConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    info = coordinator._last_service_info  # noqa: SLF001 - the coordinator keeps the last advertisement here
    data = coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "bluetooth": {
            "scanners_connectable": bluetooth.async_scanner_count(hass, connectable=True),
            "scanners_total": bluetooth.async_scanner_count(hass, connectable=False),
            "address_present_connectable": bluetooth.async_address_present(hass, coordinator.address, True),
        },
        "last_advertisement": None
        if info is None
        else {
            "name_prefix": (info.name or "")[:10],
            "source": info.source,
            "rssi": info.rssi,
            "connectable": info.connectable,
            "service_uuid_count": len(info.service_uuids),
            "withings_uuid_present": any("-5749-5448-" in u for u in info.service_uuids),
            "withings_manufacturer_data_present": WITHINGS_COMPANY_ID in info.manufacturer_data,
            "fd77_service_data_present": FD77 in {k.lower() for k in info.service_data},
            "new_data": has_new_data(info, coordinator.model_id),
        },
        "last_poll_successful": coordinator.last_poll_successful,
        "users": [
            {
                "index": i,
                "guest": uid == 0,
                "measured_at": data.users[uid].measured_at.isoformat() if uid in data.users else None,
                "measurement_types": sorted(data.users[uid].values) if uid in data.users else [],
            }
            for i, uid in enumerate(coordinator.known_users)
        ],
        "last_sync": data.last_sync.isoformat() if data.last_sync else None,
        "battery": data.battery,
        "firmware": data.firmware,
    }
