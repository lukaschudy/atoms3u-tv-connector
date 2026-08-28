# ATOMS3U Wi-Fi TV connector firmware

This firmware turns the M5Stack ATOMS3U into a small network-controlled TV
remote. Room Agent sends authenticated, bounded macros over the local network;
the compile-time profile delivers them through USB HID or exactly one selected
IR emitter.

```text
Room Agent host -- signed HTTP --> ATOMS3U -- USB HID --> compatible TV
                                      |------ Sony IR --> Bravia TV
                                      |------ LG IR ----> LG TV
                                      `------ LG IR + USB HID -> LG webOS
```

## Fast path

Use the repository-level guided setup instead of editing headers or running raw
PlatformIO commands:

```bash
.venv/bin/python room_agent/setup_tv_connector.py prepare
.venv/bin/python room_agent/setup_tv_connector.py doctor
.venv/bin/python room_agent/setup_tv_connector.py flash --emitter built-in
```

On Windows PowerShell, replace `.venv/bin/python` with
`.\.venv\Scripts\python.exe` and use backslashes in repository paths.

On a new computer, `scripts/setup.ps1` (Windows) or `scripts/setup.sh`
(macOS/Linux) creates an isolated Python environment, installs PlatformIO, and
starts `prepare`. See the [root quick start](../../../README.md) for hardware,
bootloader, discovery, and first-command instructions.

### Upgrade from protocol v1

Protocol v2 adds authenticated execution responses. Before sending a command,
the host checks `/health` for the v2 response-authentication capability. It
refuses older firmware before any USB or infrared action can run, preventing an
unsafe retry after an old unsigned response.

If the host asks you to reflash, enter download mode and run:

```bash
.venv/bin/python room_agent/setup_tv_connector.py flash --emitter built-in
```

Use `--emitter u002` instead when that is the wired emitter. Do not retry the TV
command until the reflash and `doctor` check have succeeded.

After every reboot, command execution remains unavailable until the connector
has obtained trusted network time. A temporary `clock_not_synchronized` response
means to verify internet/NTP reachability and retry later; never set connector
time from a captured command request. The firmware also generates a fresh
`boot_id`; every signed plan must carry the current value advertised by
`/health`, so a signed request captured before reboot is rejected afterwards.

## Production architecture

Production deliberately contains only the bounded components included in the
current compile and host-side test contract:

- USB keyboard and consumer-control HID;
- Sony and LG television IR transmission through either the built-in emitter
  on GPIO 12 or the optional Grove Unit IR U002 transmitter on GPIO 2;
- asynchronous 2.4 GHz Wi-Fi connection and reconnect;
- HTTP `/health` and authenticated `/v1/execute` requests and responses;
- UDP discovery on port `8781`.

The USB interfaces are registered first and started once. Wi-Fi starts
afterwards and never blocks USB initialization while waiting for a network.

Bluetooth/NimBLE is entirely excluded from the production image because it has
not implemented or passed the authenticated protocol-v2 contract. USB/Bluetooth
coexistence has not been physically accepted for this release. RGB feedback and
button diagnostics are not implemented in the current production image.

## Hardware evidence and remaining acceptance

Two minimal diagnostic images are included:

| Environment | Contents | Current evidence |
| --- | --- | --- |
| `atoms3u-hid-diagnostic` | USB keyboard + consumer control only | Compile-only CI build |
| `atoms3u-network-diagnostic` | HID + async Wi-Fi + HTTP health + UDP discovery | Compile-only CI build |

These builds validate compilation only; they do **not** validate the shared
network architecture or USB path on physical hardware. Each selected output profile
must still be cold-booted, observed for stability and exercised with a signed
command before a demo. No physical ATOMS3U or television acceptance result is
recorded for this release. The built-in emitter's optical opening is on the long
side beside the white Grove socket; aim that opening at the receiver. The
optional U002 has its own placeable emitter and a rated range of up
to 5 m, but still needs line of sight and physical testing. The remaining LG
keys still need the acceptance tests below.

## PlatformIO environments

`platformio.ini` provides five firmware environments plus one host-native
contract-test environment:

- `atoms3u-tv-connector` — production firmware (`src/main.cpp`);
- `atoms3u-tv-connector-u002` — the same production firmware using the optional
  Unit IR U002 emitter;
- `atoms3u-hid-diagnostic` — minimal USB isolation image;
- `atoms3u-network-diagnostic` — USB plus network isolation image.
- `atoms3u-ir-learning` — temporary U002 receiver image that exposes the last
  decoded remote command through `/health` for model-specific calibration.
- `native-contract-tests` — host-native schema, replay-cache, and response-HMAC
  contracts; it is not firmware and is excluded from default firmware builds.

The learning image is not the production remote. Flash it temporarily, power
the connector near the TV, press one button on the original remote, record the
decoded protocol/address/command, and then restore the production U002 image.
It stores no credentials beyond the existing private firmware settings and
does not transmit IR.

All five firmware environments use the real M5Stack board definition and
programmable TinyUSB device mode:

```ini
board = m5stack-atoms3
ARDUINO_USB_MODE=0
ARDUINO_USB_CDC_ON_BOOT=0
```

Mode `0` is required for programmable HID. CDC-on-boot is disabled so the
keyboard and consumer-control descriptors can be registered before the single
`USB.begin()` call.

## Production behavior

- `generic-usb-hid-v1`: arrows, Enter, printable US-layout ASCII text and USB
  consumer controls for Home, Back, Search, play/pause, mute, volume and power.
- `sony-bravia-ir-v1`: Sony SIRC power, Home, Back, Input, arrows, Enter,
  explicit play and pause, mute, volume and an experimental Netflix command.
  Home, Back, discrete power, media and Netflix can vary by Bravia generation
  and must be confirmed on the exact television.
- `lg-tv-ir-v1`: LG TV power, Home, Back, Input, arrows, Enter, explicit play
  and pause, mute and volume. It has no direct app or text-search command.
  Frames use NEC address `0x04` and 50% carrier duty. One-shot keys use three
  protocol repeats; volume/navigation steps remain single-frame, with a 50 ms
  gap between adjacent commands. Power Toggle, Home `0x7C`, and discrete power
  `0xC4`/`0xC5` require exact-model physical testing and remain experimental.
- `lg-webos-hybrid-v1`: bounded LG infrared power, input, media and volume,
  plus USB HID Home, Back, Search, arrows, Enter and printable ASCII text. It requires both
  `ir_ready` and `usb_ready` and is intended for opening and searching smart-TV
  apps such as YouTube without a model-specific app IR code.
- HMAC-SHA256 authentication compatible with `protocol.py`.
- 30-second timestamp window and bounded nonce replay cache.
- Strict JSON schema, at most 32 steps and at most 20 seconds of total waits.
- HTTP `/health` and `/v1/execute` endpoints.
- UDP discovery broadcasts on port `8781` after Wi-Fi receives an address.
- Generic USB execution returns `usb_not_mounted` until a USB host enumerates
  HID. Pure IR execution does not require USB data; the LG hybrid profile
  requires both IR and mounted USB HID.

The selected profile is compiled into `include/secrets.h`. A request whose
profile does not match the firmware is rejected. IR profiles also reject text
and unsupported keys before emitting anything; an `executed` response confirms
transmission, not a state acknowledgement from the television.

`/health` reports:

- selected profile, transport and profile-level `control_ready` state;
- IR availability, readiness, selected output, pins, carrier duty, send count
  and last emitted key;
- USB driver, mount, suspend and ready state;
- mount, unmount, suspend and resume counters;
- retained boot count, reset reason and uptime;
- Wi-Fi signal strength and local IP address.

This telemetry distinguishes network, USB and IR readiness. The USB counters
remain diagnostic telemetry when an IR profile is powered from USB, but USB
mounting is not a prerequisite for IR control.

The LG profile reports `transport: wifi-to-lg-nec-ir` and
`ir_protocol: lg-nec`. There are no universal LG app shortcuts in this profile;
an application launch requires a validated native-network adapter or a future
model-specific IR profile.

## Prepare private settings

From the repository root, run the guided setup:

```bash
.venv/bin/python room_agent/setup_tv_connector.py prepare
```

The setup asks for a supported profile, 2.4 GHz Wi-Fi name, and Wi-Fi password.
It creates the ignored firmware settings and host key, keeps the two keys in
sync, and never prints either secret. Check the result safely:

```bash
.venv/bin/python room_agent/setup_tv_connector.py doctor
```

For automation, all non-secret values can be flags and the password can come
from an environment variable:

```bash
TV_WIFI_PASSWORD="change-me" .venv/bin/python room_agent/setup_tv_connector.py prepare \
  --non-interactive \
  --profile sony-bravia-ir-v1 \
  --ssid "TV-Network" \
  --password-env TV_WIFI_PASSWORD
```

Use `lg-tv-ir-v1` for the experimental LG profile or
`generic-usb-hid-v1` when targeting a TV that accepts USB HID. Advanced users
can inspect the lower-level commands with
`.venv/bin/python room_agent/configure_connector.py --help` (or the Windows
virtual-environment interpreter shown above).

Never commit `include/secrets.h` or `tv-connector.key`.

## Select the physical IR output

The television profile and physical emitter are deliberately independent.
Existing installations keep using the built-in GPIO 12 emitter. The optional
M5Stack Unit IR U002 connects to the AtomS3U's white Grove/HY2.0-4P socket:

| Wire | AtomS3U | Unit IR U002 |
| --- | --- | --- |
| Black | GND | GND |
| Red | 5 V | 5 V |
| Yellow | GPIO 2 | IR TX |
| White | GPIO 1 | IR RX, currently disabled |

Disconnect AtomS3U power before attaching or removing the Grove cable. The
connector is keyed; do not force or reverse it. Firmware uses only one emitter
at a time, so a command cannot be duplicated through both LEDs. The U002
receiver remains disabled in production firmware. Use the existing
`atoms3u-ir-learning` diagnostic temporarily when capturing a model-specific
remote code, then restore a production environment.

## Build

Build the production image from the repository root:

```powershell
.\.venv\Scripts\pio.exe run `
  -d .\room_agent\firmware\atoms3u_tv_connector `
  -e atoms3u-tv-connector
```

That environment preserves today's built-in emitter. Build the prepared U002
variant instead after connecting the external module:

```powershell
.\.venv\Scripts\pio.exe run `
  -d .\room_agent\firmware\atoms3u_tv_connector `
  -e atoms3u-tv-connector-u002
```

No PC, iPhone, authentication or voice-command setting changes between these
builds. After flashing the U002 variant, `/health` must report
`ir_output: grove-unit-ir-u002`, `ir_pin: 2`, `ir_ready: true` and
`ir_carrier_duty_percent: 50`, `ir_receiver_enabled: false`. These fields prove
the selected firmware path,
not physical receipt by the television.

Build a diagnostic image only when isolating a regression:

```powershell
.\.venv\Scripts\pio.exe run `
  -d .\room_agent\firmware\atoms3u_tv_connector `
  -e atoms3u-hid-diagnostic

.\.venv\Scripts\pio.exe run `
  -d .\room_agent\firmware\atoms3u_tv_connector `
  -e atoms3u-network-diagnostic
```

## Flashing behavior

The ROM serial/COM port is used to upload an image. After flashing, unplug and
reconnect the board to cold-boot the application.

Because CDC-on-boot is disabled, the flashing COM port normally disappears once
the application is running. The host should instead retain stable HID keyboard
and consumer-control devices. One enumeration followed by a stable device is
healthy; repeated connect/disconnect events every few seconds indicate a reset
or USB re-enumeration loop.

## Hardware acceptance order

1. Cold-boot production and discover it with
   `configure_connector.py discover --timeout 10`.
2. Confirm `/health` returns `status: ok`, `configured: true`, the intended
   profile and `control_ready: true`.
3. Observe it for at least 30 seconds; uptime must increase without a new boot.
4. For generic HID, require `usb_ready: true`, then test Right, Enter and text.
5. For Sony IR, require `ir_ready: true`, aim the emitter at the receiver and
   test Mute twice, Volume Up, Home, an arrow and Enter. Test Power last.
6. For LG IR, follow the same harmless Mute/Volume/navigation sequence, then
   test Home, Back, Input and explicit Pause/Play. Test Power last and record
   every result. All exact-model LG physical results are pending.
   Phone-camera flicker is optional because some cameras filter IR completely.
7. For a U002 build, first confirm `/health` reports the external output and
   GPIO 2. Place the module at the intended mounting position and repeat the
   harmless sequence before testing Power. Confirm a requested Volume Up count
   of three produces exactly three logical steps rather than a held-key burst.

TV USB and IR support are model-specific. An IR profile can run from a
charger, extension cable or power bank and does not require USB enumeration,
but it does require optical reach. Both transports are one-way: successful
execution confirms delivery by the connector, while visible TV behavior is the
acceptance result.

## External emitter reference

- [M5Stack Unit IR U002](https://docs.m5stack.com/en/unit/ir) — 940 nm
  transmitter, 38 kHz receiver, Grove cable included and rated range up to 5 m.
- [AtomS3U Grove pin map](https://docs.m5stack.com/en/core/AtomS3U) — yellow
  wire GPIO 2 and white wire GPIO 1.
