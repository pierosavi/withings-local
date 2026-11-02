"""Connect to a waking Withings scale and inventory its GATT table (read-only).

Usage: uv run tools/gatt_dump.py [timeout_seconds]
Run it, then wake the scale (short button press). The script connects as soon as it sees a
Withings advertisement (the Withings service UUID or 0xFD77 service data) and lists services,
characteristics and descriptors. It writes nothing and subscribes to nothing; it only reads
characteristics in the standard Device Information and Battery services, so it shouldn't trigger
pairing.
Output: captures/ble/gatt-<timestamp>.json (git-ignored: may include serial numbers).
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner

from withings_wpp.advertisement import is_withings

SAFE_READ_SERVICES = {"0000180a", "0000180f"}  # Device Information, Battery
OUT = Path(__file__).resolve().parent.parent / "captures" / "ble"


async def main(timeout: float) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f">>> Waiting up to {timeout:.0f}s. Short-press the scale's button now. <<<\n", flush=True)
    found = asyncio.get_running_loop().create_future()

    def on_adv(device, adv):
        if is_withings(adv.service_uuids, adv.service_data) and not found.done():
            found.set_result((device, adv))

    async with BleakScanner(on_adv):
        try:
            device, adv = await asyncio.wait_for(found, timeout)
        except TimeoutError:
            print("No Withings advertisement seen.")
            return
    print(f"Found {adv.local_name!r} ({adv.rssi} dBm), connecting...", flush=True)

    result = {"name": adv.local_name, "advertised_uuids": adv.service_uuids, "services": []}
    async with BleakClient(device, timeout=20) as client:
        print(f"Connected (MTU {client.mtu_size}).", flush=True)
        for svc in client.services:
            s = {"uuid": svc.uuid, "description": svc.description, "characteristics": []}
            for ch in svc.characteristics:
                c = {
                    "uuid": ch.uuid,
                    "description": ch.description,
                    "properties": ch.properties,
                    "descriptors": [{"uuid": d.uuid, "description": d.description} for d in ch.descriptors],
                }
                if svc.uuid[:8] in SAFE_READ_SERVICES and "read" in ch.properties:
                    try:
                        raw = await client.read_gatt_char(ch)
                        c["value_hex"] = raw.hex()
                        c["value_text"] = raw.decode("utf-8", "replace")
                    except Exception as e:  # noqa: BLE001 - record and keep going
                        c["read_error"] = repr(e)
                s["characteristics"].append(c)
            result["services"].append(s)

    path = OUT / f"gatt-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(result, indent=2))
    for s in result["services"]:
        print(f"\nService {s['uuid']}  ({s['description']})")
        for c in s["characteristics"]:
            val = f"  = {c['value_text']!r}" if "value_text" in c else ""
            print(f"  {c['uuid']}  [{', '.join(c['properties'])}]  {c['description']}{val}")
    print(f"\nSaved {path}")


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 90))
