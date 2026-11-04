"""Read stored measurements from the scale after a weigh-in (read-only).

Usage: uv run tools/read_measurements.py

Needs secrets/scale.json from tools/fetch_kl.py; the model saved there picks the scale (Body Smart
or Body Comp). Step on the scale; when it advertises (about a minute later) the script connects,
authenticates with kl, asks for the stored measurements and disconnects. It never deletes
measurements and never changes settings, so the Withings app can still sync the same readings
afterwards. On a Body Comp it doesn't send SYNC_OK either.

The scale must be in Bluetooth mode and paired with this computer (on Linux:
`bluetoothctl`, `agent KeyboardDisplay`, `pair <address>`, then type the code the scale shows).

Measurements are personal health data. They are printed here and the raw exchange is saved to
captures/ble/ (git-ignored). Never commit either.
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner

from withings_wpp.client import ScaleClient, WppCommandError, WppSession
from withings_wpp.devices import BODY_SMART, PROFILES, profile_for
from withings_wpp.measures import merge_by_time
from withings_wpp.messages import ConnectReason

ROOT = Path(__file__).resolve().parent.parent
REASONS = {v: k for k, v in vars(ConnectReason).items() if not k.startswith("_")}


async def main() -> None:
    secret = json.loads((ROOT / "secrets" / "scale.json").read_text())
    kl = secret["kl"]
    profile = PROFILES.get(secret.get("model"), BODY_SMART)
    print(f">>> {profile.name}: waiting up to 3 min. Step on the scale now and wait for the full reading. <<<\n", flush=True)
    found = asyncio.get_running_loop().create_future()

    def on_adv(device, adv):
        if profile_for(adv.service_uuids, adv.service_data, adv.local_name) is profile and not found.done():
            found.set_result(device)

    async with BleakScanner(on_adv):
        device = await asyncio.wait_for(found, 180)

    start = time.monotonic()
    trace = []

    def record(direction, raw):
        trace.append({"t": round(time.monotonic() - start, 3), "dir": direction, "hex": raw.hex()})

    try:
        async with BleakClient(device, timeout=20) as ble:
            session = WppSession(ble, trace=record, profile=profile)
            await session.start()
            scale = ScaleClient(session)
            result = await scale.authenticate(kl)
            if not result.authenticated:
                print("Scale didn't ask for authentication (button/setup session). Weigh in instead.")
                return
            if not result.scale_verified:
                print("✗ The device didn't prove it knows kl; not reading from it.")
                await scale.disconnect()
                return
            print("✓ Authenticated (and the scale proved it knows kl too)", flush=True)
            try:
                reason = await scale.connect_reason()
                print(f"  connect reason: {reason} ({REASONS.get(reason, '?')})", flush=True)
            except WppCommandError as e:
                print(f"  connect reason refused: {e}")
            except TimeoutError:
                print("  connect reason timed out; continuing with stored measurements")
            status = await scale.measure_status()
            print(f"  stored measurement groups: {status.count}", flush=True)
            groups = merge_by_time(await scale.measures()) if status.count > 0 else []
            for g in groups:
                when = g.time.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                values = ", ".join(f"{m.name}={m.value}{m.unit and ' ' + m.unit}" for m in g.measures)
                extra = f" (+ unparsed TLVs {sorted(set(g.unknown_tlvs))})" if g.unknown_tlvs else ""
                print(f"  {when}  {values}{extra}")
            if session.sync_ok:
                try:
                    await scale.sync_ok()
                    print("  sync acknowledged (SYNC_OK)")
                except (WppCommandError, TimeoutError) as e:
                    print(f"  SYNC_OK not acknowledged: {e}")
            await scale.disconnect()
            await asyncio.sleep(0.5)
    finally:
        out = ROOT / "captures" / "ble" / f"session-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(trace, indent=2))
        print(f"\nRaw exchange saved to {out.relative_to(ROOT)} (contains health data: do not commit)")


if __name__ == "__main__":
    asyncio.run(main())
