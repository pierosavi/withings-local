# Changelog

Notable changes to the Withings Local integration. Versions follow [semantic versioning](https://semver.org); each one is a GitHub release that HACS offers as an update.

## [Unreleased]

## [0.4.0] - 2026-10-29

### Added
- **Support for Body (WBS06)**, also sold as Nokia Body ([#3](https://github.com/pierosavi/withings-local/pull/3), thanks @lientry). It sends its readings over Bluetooth Classic rather than Bluetooth Low Energy, so Home Assistant listens for the scale's connection after a weigh-in instead of polling it. Setup pairs without a code. It needs a local Bluetooth adapter on Linux (BlueZ); ESPHome Bluetooth proxies can't do it. Sensors: weight and last weigh-in per user, plus battery and last sync. See [docs/wbs06.md](docs/wbs06.md).

### Changed
- Fetching the key with your Withings login now picks the scale that matches the one being set up, by model and Bluetooth address. When an account has several scales of the same model and none matches, setup says so instead of taking the first one; enter the key yourself in that case.
- `tools/fetch_kl.py` takes `--model 7` for the Body, and stops instead of guessing when the account has several scales of that model.

## [0.3.3] - 2026-10-24

### Changed
- A device that fails to prove it knows the key gets no further commands: nothing is read from it and no sync acknowledgement is sent to it. Before, its readings were read and then ignored.

### Fixed
- Messages a device sends on its own, or that are left over from an earlier request, are no longer mistaken for the answer to a request. Some Withings devices send several right after connecting.
- A stored measurement record that can't be decoded is skipped instead of failing the whole sync, and values that follow it are no longer attached to the previous weigh-in.

## [0.3.2] - 2026-10-17

### Added
- Discovery logs other Withings devices it doesn't support (name and model ID, not the address) at debug level, and the README explains how to share that to help add a device.

### Changed
- New integration icon: a white scale with a Bluetooth display.
- The README and description now say the integration supports the Withings Body Smart.

## [0.3.1] - 2026-10-16

### Changed
- Readings are ignored when the device fails to prove it knows the key (mutual authentication). Before, they were recorded with a warning. A real scale always passes this check; a device impersonating the scale's advertisement can't.

### Fixed
- An unexpected error during pairing (for example when the system D-Bus can't be reached) shows the "Pairing failed" step with the error and a retry, instead of "Unknown error".

## [0.3.0] - 2026-10-16

### Added
- **Pairing during setup.** The scale only shares measurements over a Bluetooth connection paired with the 6-digit code on its display. Setup now asks you to hold the scale's button, waits for its setup-mode signal, and asks for the code. Home Assistant registers a temporary BlueZ pairing agent for this, and the pairing is stored on the host. Tested on a Raspberry Pi 4 with Home Assistant OS.
- **Reauthentication when pairing is lost.** If the scale refuses a connection because it isn't paired (for example after a battery change), Home Assistant raises "Reauthentication required" and walks you through pairing again. You can also re-pair at any time with Reconfigure.

### Fixed
- When the scale drops the Bluetooth link mid-session, the sync fails at once and retries on the next advertisement, instead of waiting for a timeout. Linux (BlueZ) leaves the pending call hanging in this case.
- Per-user devices link to the scale with `via_device_id`. The old `via_device` form is deprecated and stops working in Home Assistant 2027.8.

### Changed
- Connection retries use the newest Bluetooth device data, and "can't connect" warnings include Home Assistant's reachability diagnostics.
- "Enable debug logging" also shows the connection retry library's attempts (`bleak_retry_connector`).

### Known limitations
- Pairing works with Bluetooth adapters attached to the Home Assistant host on Linux. ESPHome Bluetooth proxies can't pair with a code, and a proxy without the pairing can't read the scale (upstream habluetooth issue #628).
- After pairing, the scale may display "Setup failed". This is harmless.

## [0.2.2] - 2026-10-16

### Fixed
- A Bluetooth session that hangs, seen on a Raspberry Pi adapter after connecting, no longer blocks every later sync until a restart. Subscribing, each write and the whole session now have timeouts.

### Changed
- Debug logs show each session step (subscribing, subscribed, every message) and when the scale disconnects.

## [0.2.1] - 2026-10-16

### Fixed
- After a failed connection, retry on the scale's next advertisement instead of waiting 30 seconds. The advertising window is short, so a weak signal used to mean a missed weigh-in.
- Scales discovered without a name (common with passive or distant adapters) are titled "Body Smart" instead of their Bluetooth address. Existing entries are renamed.

### Changed
- Every failed sync logs a warning with the step, elapsed time, adapter and signal strength, not just the first failure.

## [0.2.0] - 2026-10-16

### Added
- **One device per Withings user** with that person's latest weigh-in. Names come from the Withings account when you set up with your login; otherwise users show as "User 1", "User 2" (rename them in Home Assistant).
- **Guest weigh-ins** get their own device, with weight only.
- **Battery level** sensor on the scale's device.
- **Diagnostics download**, with the key, address and user names removed.
- **Debug logging** of every advertisement and sync, to troubleshoot Bluetooth reachability.
- **Integration icon.**

### Changed
- Weigh-ins are de-duplicated by time and measurement type, since the scale re-sends its whole store on every sync.
- Sessions end with `SYNC_OK`, as the scale expects. Verified to delete nothing: the Withings app still receives the same readings.
- A weigh-in is also detected from the manufacturer data, so adapters and proxies that scan passively work too.

### Fixed
- User IDs in stored records are read correctly; only the valid ones are used.
- On the login and manual-key steps, submitting empty fields goes back to the choice of key method.

### Upgrade notes
- The single-user sensors from 0.1.0 are removed automatically. New per-user sensors appear after the next weigh-in.

## [0.1.0] - 2026-10-16

### Added
- First version: Bluetooth discovery of the Body Smart (WBS13), key setup (Withings login or pasted `kl`), and sensors for the latest weigh-in.
