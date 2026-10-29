# Withings Local (Bluetooth)

Get the readings from your Withings scale into Home Assistant **over Bluetooth, locally**, without going through the Withings cloud. Body Smart (WBS13) and Body (WBS06) are supported; other devices aren't supported yet. See [Help add your device](#help-add-your-device).

On Body Smart, step on the scale, and a minute later your weight, body fat, muscle mass and more show up as sensors in Home Assistant, ready for dashboards, history graphs and automations.

> **Status: working, early.** I use it with a Body Smart (WBS13), and the values match the Withings app. My setup is Home Assistant OS on a Raspberry Pi 4, using the Pi's built-in Bluetooth. I haven't tested other setups yet, so if you try one, let me know how it goes in the [issues](https://github.com/pierosavi/withings-local/issues). Known limitations are in the [changelog](CHANGELOG.md).

## What you get

One device in Home Assistant for each person who uses the scale, with these sensors:

- weight, fat mass, fat ratio, fat-free mass, muscle mass, bone mass, water mass
- visceral fat, basal metabolic rate, metabolic age, heart rate
- time of the last weigh-in

Guest weigh-ins go to a separate "Guest" device (weight only). The scale itself also gets a device, showing its battery level and the time of the last sync.

**[Body (WBS06)](docs/wbs06.md):** weight and last weigh-in per user, plus battery and last sync.

## Before you start

You need:

- **A Withings Body Smart (WBS13)** or **Body (WBS06)**. Other Withings devices aren't supported yet.
- **Home Assistant 2026.10 or newer.**
- **A Bluetooth adapter on the Home Assistant machine**, within a few meters of the scale. The built-in Bluetooth of a Raspberry Pi works, and so does a USB Bluetooth dongle. Body (WBS06) specifically needs a Bluetooth Classic (BR/EDR) adapter on Linux with BlueZ. ESPHome Bluetooth proxies are *not* enough on their own: pairing needs a local adapter, and WBS06 also needs an incoming Classic connection.
- **[HACS](https://hacs.xyz)** (the Home Assistant Community Store), to install and update the integration. If you don't have it yet, follow the [HACS setup guide](https://hacs.xyz/docs/use/) first.
- **Your Withings account** (the one you use in the Withings app). Setup uses it once to get your scale's key. Your password is not stored.

## Step 1: switch the scale to Bluetooth mode

Both Body (WBS06) and Body Smart (WBS13) send readings to Withings over Wi-Fi by default. To receive readings locally, switch the scale to Bluetooth mode:

1. In the Withings app, open your scale's page.
2. Tap **Wi-Fi configuration**.
3. Press and hold the button under the scale for about **3 seconds**. The configuration screen appears on your phone.
4. Tap **Other options**, then **I don't have a Wi-Fi**.

The Withings app keeps working: it still picks up your readings whenever your phone is near the scale. To go back to Wi-Fi mode later, repeat these steps and pick your Wi-Fi network. (Tested with Withings app 26.40.)

A **short press** opens the on-scale menu, and a **3-second hold** starts configuration with the app. Avoid the **Reset** entry in the on-scale menu: it wipes the scale's setup.

## Step 2: install with HACS

The quickest way is this button, which opens the repository in HACS on your own Home Assistant:

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=pierosavi&repository=withings-local&category=integration)

Or add it by hand:

1. In Home Assistant, open **HACS** from the sidebar.
2. Click the **⋮** menu in the top-right corner and choose **Custom repositories**.
3. Paste `https://github.com/pierosavi/withings-local` as the repository, choose **Integration** as the type, and click **Add**.
4. Search HACS for **Withings Local (Bluetooth)**, open it, and click **Download**.

Then **restart Home Assistant** (**Settings → System → power button (top right) → Restart Home Assistant**). New integrations only load after a restart.

<details>
<summary>Installing without HACS</summary>

Copy the `custom_components/withings_local` folder from this repository into the `custom_components` folder inside your Home Assistant configuration folder (create it if it doesn't exist), then restart Home Assistant. You'll need to repeat this by hand for every update.
</details>

## Step 3: add the scale to Home Assistant

1. **Step on the scale.** After a weigh-in, the scale announces itself over Bluetooth for a short while, and Home Assistant shows it under **Settings → Devices & services** as a discovered device. Click **Add**.

   If nothing shows up, step on the scale again and, right after, go to **Settings → Devices & services → Add integration** and search for **Withings Local (Bluetooth)**.

   [![Open your Home Assistant instance and start setting up Withings Local.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=withings_local)

2. **Give it the scale's key.** Pick **Fetch the key with my Withings login** and enter your Withings email and password. They are sent to Withings once to read the key, and not stored anywhere. (If you'd rather not, you can get the key yourself with [`tools/fetch_kl.py`](tools/fetch_kl.py) and choose **Enter the key myself**.)

3. **Pair with the scale.** This only happens once:
   1. Choose **Pair now** in Home Assistant and hold the button under the scale for **3 seconds** to enter setup mode.
   2. **Body Smart (WBS13):** Type in the 6-digit code shown on the scale's display. You have about 30 seconds. **Body (WBS06):** No code is needed; Home Assistant pairs automatically.

   The scale may then show **"Setup failed"**. Check whether pairing succeeded in Home Assistant and confirm it with a new weigh-in; the display message alone doesn't tell you whether synchronization works.

4. **Weigh in.** About a minute after the reading finishes, the sensors update.

Each person gets their own device, named **Body Smart (name)** or **Body (name)**. If you set up with your Withings login, the names come from your account; with a pasted key they're called "User 1", "User 2" and so on, and you can rename them in Home Assistant.

## Updating

HACS tells you when a new version is out: you'll see an update under **Settings → Updates**, like for Home Assistant itself. Install it and restart. What changed in each version is in the [changelog](CHANGELOG.md).

## Troubleshooting

- **A weigh-in didn't show up.** If your phone's Withings app reaches the scale first, it uploads the readings and then deletes them from the scale, so Home Assistant can miss them. Keep the Bluetooth adapter close to the scale. (Home Assistant never deletes anything, so the Withings app still gets every reading.)
- **"Reauthentication required".** The pairing was lost, for example after you changed the scale's batteries. Click the notification and pair again (step 3 above). You can also re-pair at any time from the integration's **⋮ → Reconfigure** menu.
- **It worked once and then stopped, and I also use ESPHome Bluetooth proxies.** Home Assistant may be connecting to the scale through a proxy, which doesn't hold the pairing. Move the scale closer to the Home Assistant machine's own Bluetooth adapter than to any proxy.
- **Never discovered at all.** Check that the scale is in Bluetooth mode (step 1), and that Home Assistant's **Bluetooth** integration is set up and sees your adapter.
- **Can I keep the scale in Wi-Fi mode?** This integration requires Bluetooth mode for local sync. On Body Smart, I tested an unpaired scale in Wi-Fi mode and saw no Bluetooth advertisement after a weigh-in; the paired case is untested. [esphome-withings-wpp](https://github.com/NavistAuLabs/esphome-withings-wpp) reports that Body Smart can keep Wi-Fi working after pairing from an ESP32.
- **Still stuck?** Turn on **Enable debug logging** on the integration's page, step on the scale, then turn it off again and attach the downloaded log to an [issue](https://github.com/pierosavi/withings-local/issues).

## How it works

1. After a weigh-in, the scale advertises over Bluetooth for a short time.
2. Body Smart connects over BLE when Home Assistant polls it. Body (WBS06) initiates a Bluetooth Classic connection to Home Assistant instead.
3. Home Assistant proves it knows the scale's key, the scale proves it knows it too, and then Home Assistant reads the stored measurements.
4. Nothing is deleted from the scale, so the Withings app still syncs the same readings when your phone is nearby.

The key (`kl`) comes from your own Withings account, once, during setup.

## Help add your device

Other Withings scales and watches use the same Bluetooth protocol family, so adding one is mostly a matter of learning how it identifies itself and what it sends. If you own one, [open an issue](https://github.com/pierosavi/withings-local/issues) with the model name (as shown in the Withings app) and whichever of these you can get.

**From Home Assistant (no extra tools).** Add this to `configuration.yaml` and restart:

```yaml
logger:
  logs:
    custom_components.withings_local: debug
```

Wake the device (step on it, or press its button), then search the logs (**Settings → System → Logs**, then **Show raw logs**) for `Unsupported Withings device` and paste that line. It contains the advertised name and model ID, not the device's address. Remove the logger lines afterwards.

**From a computer with Bluetooth (more detail).** With the repository cloned and [uv](https://docs.astral.sh/uv/) installed, wake the device right after starting each command:

1. `uv run tools/ble_scan.py 60 --press-at 10`: what the device advertises.
2. `uv run tools/gatt_dump.py`: its Bluetooth services. Read-only, no pairing.
3. `uv run tools/probe.py`: its reply to a hello message. Read-only; a scale in setup mode may show "Setup failed", which is harmless.

These tools inspect **BLE**. Failure to connect with GATT does not rule out a different transport. On Linux, `bluetoothctl info AA:BB:CC:DD:EE:FF` can show a previously discovered device's Classic Bluetooth services without pairing or changing its configuration. Body (WBS06), for example, synchronizes through incoming RFCOMM rather than GATT. Include the model and transport observations when reporting a new device, but redact its address.

Paste the console output, after replacing the device's Bluetooth address (`AA:BB:CC:DD:EE:FF`) and any serial number with `xx`. `probe.py` already hides them, but the scan and the GATT dump print them. Don't attach the files these tools save under `captures/`: they contain identifiers, and they can contain measurements.

## For developers

| Path | What |
|---|---|
| `custom_components/withings_local/` | Home Assistant integration: Bluetooth discovery, setup, sensors for the latest weigh-in |
| `src/withings_wpp/` | Python library (`withings-wpp`): clean-room client for the scale's Bluetooth protocol, read-only |
| `tools/` | CLI helpers: scan, probe, `fetch_kl.py` (get your scale's key), `read_measurements.py` |
| `docs/` | [Protocol notes](docs/protocol.md), [recon log](docs/recon.md) |

```bash
uv sync --group ha
uv run pytest
uv run tools/ble_scan.py 60   # run from your own terminal, then step on the scale
```

On macOS, Bluetooth scripts need Bluetooth permission for the terminal app that runs them.

## Prior art and thanks

This project builds on public work by others. I read these projects to understand the protocol; no code was copied from them.

- [openwithings](https://github.com/totruok/openwithings) (MIT): protocol notes and tools for the Body+ (WBS05). My starting point for the message framing, the probe and challenge exchange, and the account-key flow.
- [esphome-withings-wpp](https://github.com/NavistAuLabs/esphome-withings-wpp): an ESPHome component that reads a Body Smart on an ESP32. Use it if you prefer a dedicated ESPHome device, or if you want to keep the scale in Wi-Fi mode.
- [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge): its Withings Steel HR support documents the same protocol family for watches.
- [withouthings](https://github.com/DavidVentura/withouthings) and [scanwatch_ble](https://github.com/SureshotM6/scanwatch_ble): the same protocol on Withings ScanWatch watches.

## Good to know

- **No Withings code.** This repository contains no Withings code, firmware or app files, and never will.
- **Not a medical device.** Values are shown as the scale reports them, with no guarantee of accuracy. A Withings firmware update could break things.

## Legal

This project exists for interoperability with hardware you own. Withings is a trademark of Withings SA. This project is not affiliated with, endorsed by or supported by Withings. Use it at your own risk.

## License

[MIT](LICENSE)
