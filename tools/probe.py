"""Send one WPP PROBE to a waking Withings scale and record everything it says back.

Usage: uv run tools/probe.py [--char PREFIX] [--app-version N] [--os-version N] [--listen SECONDS]

By default PROBE goes to the WPP characteristic `00000024-…` (Body Smart, Body). Newer devices
such as the BeamO use `00000004-…` in the service ending in ASCII "WITHINGS": pass `--char 00000004`.

Safe by design. The script sends only PROBE, optionally CONNECT_REASON and STORED_MEASURE action 0
(both reads), and DISCONNECT.
It never answers device requests such as SETUP_OK,
so a setup-mode session ends with "Setup failed" on the display and nothing on the scale changes.
If the link needs encryption, macOS shows a pairing dialog: type the code shown on the scale.

The console redacts identifiers (MAC, secret, manufacturing ID). Raw fragments are saved to
captures/ble/probe-<timestamp>.json, which is git-ignored: never commit it.
"""

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner

from withings_wpp.codec import Message, Reassembler
from withings_wpp.advertisement import is_withings
from withings_wpp.messages import (
    Cmd,
    CmdError,
    ConnectReason,
    MeasureAction,
    Obj,
    ProbeChallenge,
    ProbeReply,
    StoredMeasureStatus,
    connect_reason_request,
    describe,
    probe,
    stored_measure,
)

WITH_MARKER = "-5749-5448-"
DEFAULT_CHAR = "00000024"
OUT = Path(__file__).resolve().parent.parent / "captures" / "ble"


def redacted(s: str) -> str:
    return f"<{len(s)} chars>"


def summarize(msg) -> list[str]:
    lines = [describe(msg)]
    for tlv in msg.tlvs:
        try:
            if tlv.type == Obj.PROBE_REPLY:
                p = ProbeReply.parse(tlv.value)
                lines.append(
                    f"    ProbeReply: vid={p.vid} pid={p.pid} name={p.name!r} mac={redacted(p.mac)} "
                    f"secret={redacted(p.secret)} mfg_id={redacted(p.mfg_id)} hw={p.hard_version} "
                    f"bootloader={p.bl_version} firmware={p.soft_version} rescue={p.rescue_version} "
                    f"extra={len(p.extra)}B"
                )
            elif tlv.type == Obj.CONNECT_REASON:
                reason = int.from_bytes(tlv.value[:2], "big")
                names = {v: k for k, v in vars(ConnectReason).items() if not k.startswith("_")}
                lines.append(f"    connect reason: {reason} ({names.get(reason, '?')})")
            elif tlv.type == Obj.STORED_MEASURE_STATUS:
                st = StoredMeasureStatus.parse(tlv.value)
                lines.append(f"    stored measures: count={st.count} wifi_configured={st.wifi_configured}")
            elif tlv.type == Obj.CMD_ERROR:
                lines.append(f"    error: {CmdError.parse(tlv.value)}")
            elif tlv.type == Obj.PROBE_CHALLENGE:
                c = ProbeChallenge.parse(tlv.value)
                lines.append(f"    ProbeChallenge: mac={redacted(c.mac)} challenge={len(c.challenge)}B extra={len(c.extra)}B")
        except Exception as e:  # noqa: BLE001 - layout guesses can be wrong; keep the raw capture
            lines.append(f"    could not parse 0x{tlv.type:04x}: {e}")
    return lines


async def main(args) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print(">>> Waiting up to 3 min for the scale. Step on it now (or short-press its button). <<<\n", flush=True)
    found = asyncio.get_running_loop().create_future()

    def on_adv(device, adv):
        if is_withings(adv.service_uuids, adv.service_data) and not found.done():
            found.set_result(device)

    async with BleakScanner(on_adv):
        try:
            device = await asyncio.wait_for(found, 180)
        except TimeoutError:
            print("No Withings advertisement seen.")
            return

    log = {"sent": [], "received": [], "events": []}
    start = time.monotonic()
    stamp = lambda: round(time.monotonic() - start, 3)  # noqa: E731
    reasm = Reassembler()
    last_rx = [time.monotonic()]

    def on_notify(_char, data: bytearray):
        last_rx[0] = time.monotonic()
        log["received"].append({"t": stamp(), "hex": bytes(data).hex()})
        try:
            for msg in reasm.feed(bytes(data)):
                for line in summarize(msg):
                    print(f"[{stamp():6.2f}s] <- {line}" if not line.startswith(" ") else line, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{stamp():6.2f}s] !! decode error: {e}", flush=True)
            log["events"].append({"t": stamp(), "decode_error": repr(e)})

    try:
        async with BleakClient(device, timeout=20) as client:
            chars = [c for s in client.services for c in s.characteristics if WITH_MARKER in c.uuid.lower()]
            char = next((c for c in chars if c.uuid.lower().startswith(args.char)), None)
            if char is None:
                found_chars = ", ".join(c.uuid for c in chars) or "none"
                raise RuntimeError(f"no Withings characteristic starting with {args.char}; found: {found_chars}")
            print(f"[{stamp():6.2f}s] connected, MTU {client.mtu_size}", flush=True)
            await client.start_notify(char, on_notify)

            async def send(msg, listen: float) -> None:
                raw = msg.encode()
                log["sent"].append({"t": stamp(), "hex": raw.hex()})
                print(f"[{stamp():6.2f}s] -> {describe(msg)}", flush=True)
                await client.write_gatt_char(char, raw, response=True)
                last_rx[0] = time.monotonic()
                while time.monotonic() - last_rx[0] < listen and client.is_connected:
                    await asyncio.sleep(0.2)

            await send(probe(args.app_version, args.os_version), args.listen)
            if args.connect_reason and client.is_connected:
                # Read-only query; the scale may refuse it before authentication.
                await send(connect_reason_request(), 3)
            if args.get_state and client.is_connected:
                # Read-only: asks how many measurements are stored (action 0). Never sends delete.
                await send(stored_measure(MeasureAction.GET_STATE), 3)
            if client.is_connected:
                # The phone app ends every session this way.
                await send(Message(Cmd.DISCONNECT), 1)
            log["events"].append({"t": stamp(), "end": "connected" if client.is_connected else "device disconnected"})
    except Exception as e:  # noqa: BLE001 - record whatever happened
        print(f"[{stamp():6.2f}s] !! {type(e).__name__}: {e}", flush=True)
        log["events"].append({"t": stamp(), "error": repr(e)})

    if reasm.pending:
        print(f"{reasm.pending} bytes left unassembled")
    path = OUT / f"probe-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(log, indent=2))
    print(f"\nSaved raw capture to {path} (do not commit)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "--char",
        type=lambda v: v.lower().removeprefix("0x").zfill(8),
        default=DEFAULT_CHAR,
        help="prefix of the WPP characteristic UUID (default 00000024; 00000004 on newer devices)",
    )
    p.add_argument("--app-version", type=int, default=202609240, help="AppProbe version field")
    p.add_argument("--os-version", type=int, default=34, help="AppProbeOsVersion, e.g. Android API level")
    p.add_argument("--listen", type=float, default=8, help="seconds to keep listening after the last message")
    p.add_argument("--connect-reason", action="store_true", help="also ask the scale why it connected")
    p.add_argument("--get-state", action="store_true", help="also ask how many measurements are stored (read-only)")
    asyncio.run(main(p.parse_args()))
