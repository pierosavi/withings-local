# Body (WBS06): local Bluetooth support

Body (WBS06) uses a different transport from Body Smart
(WBS13). Both models speak Withings' WPP protocol, but WBS06
initiates **Bluetooth Classic RFCOMM** connections after a weigh-in.
Body Smart uses **Bluetooth Low Energy GATT**.

## Requirements

- Home Assistant 2026.10 or newer, running on Linux with a **local
  BR/EDR-capable Bluetooth adapter** and BlueZ.
- Access to the scale's existing Withings `kl` secret, through the
  integration's Withings login flow or manual key entry. The optional
  `uv run tools/fetch_kl.py --model 7` helper retrieves it from
  your own account; it refuses ambiguous accounts with several WBS06 scales.
- Switch the scale to Bluetooth mode through the Withings app, using the
  same [steps as Body Smart](../README.md#step-1-switch-the-scale-to-bluetooth-mode).
- Classic pairing requires permissions for the Linux Bluetooth
  management control socket and BlueZ's D-Bus pairing-agent API.
  ESPHome Bluetooth proxies cannot supply this Classic host service.
  If Home Assistant cannot register the RFCOMM listener, the
  integration reports setup failure instead of pretending WBS06
  can synchronize through a BLE fallback.

Follow the shared [Home Assistant setup instructions](../README.md#step-3-add-the-scale-to-home-assistant).
For WBS06, Home Assistant establishes the Classic bond without a passkey.
The scale must stay in setup mode while pairing completes.

The scale may display **"Setup failed"** after pairing. This occurred
in the hardware test despite successful Home Assistant
pairing and a subsequent authenticated weigh-in. Treat Home Assistant's
pairing result, the stored Classic bond and an actual new measurement as
the success criteria; the scale display alone does not prove success.

If the Bluetooth bond already exists, the setup flow also offers
*Skip (already paired)*. This checks BlueZ for an existing BR/EDR
bond without modifying it; the flow refuses to finish if the bond
cannot be verified. Pairing is never attempted automatically during
measurement synchronization.

## How synchronization works

1. Home Assistant registers one pair of WBS06-specific SDP service
   UUIDs as native BlueZ RFCOMM server profiles, shared across all
   configured WBS06 scales. Incoming connections are routed
   by the scale's Bluetooth address. If BlueZ restarts or unregisters
   a profile, the server retries registration until it recovers
   or the integration unloads.
2. The transport receives a **FragFsm RESET** (`01 00`), then carries
   WPP packets in DATA frames (`01 01`, two-byte big-endian length,
   WPP bytes). The transport reassembles partial or combined frames.
3. The shared WPP client sends its probe, completes the challenge
   exchange with the `kl` secret, verifies the device, and reads
   stored measurements without changing the scale's settings.
4. The existing coordinator merges measurements by timestamp and
   publishes the latest weight and weigh-in time per user. The
   scale-level device exposes battery and last successful sync.

The implementation uses Python's standard-library `socket` module
and Home Assistant's existing `dbus-fast` dependency. FragFsm is a
small internal protocol module, not an external dependency.

## Model-specific behavior

| | Body (WBS06) | Body Smart (WBS13) |
|---|---|---|
| Measurement transport | Incoming Classic RFCOMM | Active BLE GATT |
| Bluetooth pairing | Just Works, BR/EDR setup mode | Six-digit LE passkey |
| Per-user sensors | Weight, last weigh-in | Full set of supported measurements |
| Scale sensors | Battery, last sync | Battery, last sync |
| Requires local BR/EDR adapter | Yes | No |

Body+ (WBS05) is different again: [openwithings](https://github.com/totruok/openwithings/blob/main/docs/protocol.md) documents its weight synchronization over BLE GATT despite the scale also advertising a Classic Serial Port service. Do not assume either WBS05 or WBS06 transport applies to other Withings models.

Battery percentage is queried during every authenticated WPP session.
If that particular query fails, the previous valid battery value is
retained. The last-sync timestamp advances after every verified
session, including sessions without new measurements.

Body (WBS06) does not provide body-composition measurements.
Consequently, the WBS06 integration does not create their sensors.
An unnamed user appears as `User 1`, `User 2`, and so forth;
account-supplied first names take precedence.

## Safety and limitations

- **No cloud dependency for measurement synchronization.** Withings
  credentials are only used if the operator chooses online key
  retrieval during setup.
- The Classic pairing routine never removes a device or erases its
  LE keys. Its temporary agent rejects unrelated devices and PIN
  entry. It restores adapter Pairable and PairableTimeout on exit,
  with a BlueZ pairing-timeout fallback.
- Incoming measurements are accepted only after application-level
  WPP challenge verification. A connected RFCOMM socket is not
  sufficient proof of device identity.
- Classic measurement sessions intentionally **do not send
  `SYNC_OK`**, pending model-specific proof that acknowledging a sync
  cannot remove stored measurements. This may leave readings queued
  for subsequent connections.
- One real scale completed the full RFCOMM, FragFsm, WPP, and Home
  Assistant sensor path, including a fresh Classic pairing.
- Fresh Classic pairing from the native Home Assistant flow was
  hardware-validated after removing both previous Bluetooth bonds
  and the original integration entry. BlueZ retained a Classic
  BR/EDR bond and trust without an LE bond; one fresh, authenticated
  weigh-in was received under the newly created entry.

## Testing

The repository's normal `uv run pytest` suite exercises the
model-specific config flow, native BlueZ pairing command encoding
and cleanup, RFCOMM service lifecycle, fragmented FragFsm frames,
authenticated WPP streams and model-specific Home Assistant sensors.
The first-install config flow and an already-bonded reinstall are
tested with simulated BlueZ. The actual initial Classic bond and
subsequent sync were also tested on one previously configured WBS06.
Simulation tests also cover two simultaneous scale connections, BlueZ restart,
recovery retries, explicit profile release, and read-only bond
inspection before allowing a pairing skip.

The physical proof of sync is based on local, privacy-limited
diagnostics. Do not upload Bluetooth addresses, `kl` secrets,
account identifiers, stored measurements, or raw HCI captures when
reporting problems.
