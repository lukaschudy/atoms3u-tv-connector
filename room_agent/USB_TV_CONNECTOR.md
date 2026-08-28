# ATOMS3U Wi-Fi TV connector

The ATOMS3U is a small, network-controlled replacement for part of a TV
remote. It joins the same Wi-Fi network as the Room Agent host and translates
authenticated commands through one explicitly selected television profile:

- `generic-usb-hid-v1` presents a USB keyboard and consumer-control device;
- `sony-bravia-ir-v1` emits Sony SIRC infrared commands through the selected
  IR output;
- `lg-tv-ir-v1` emits the bounded LG television infrared command set through
  the same selected output.
- `lg-webos-hybrid-v1` combines LG infrared remote keys with USB HID search,
  navigation, confirmation and printable text entry.

```text
iPhone / room microphone -> Room Agent host -> signed HTTP macro
                                           -> ATOMS3U over Wi-Fi
                                              |-> USB HID -> compatible TV
                                              |-> Sony IR -> Bravia TV
                                              |-> LG IR ---> LG TV
                                              `-> LG IR + USB HID -> LG webOS
```

The generic profile needs a USB **host/data** port that enumerates HID. The IR
profiles need only power and optical line of sight to the TV's IR receiver; the
television itself can remain offline. A short USB extension, charger or power
bank is often more useful than hiding the emitter behind the panel.

IR firmware has two mutually exclusive production builds:

- `atoms3u-tv-connector` uses the built-in side emitter on GPIO 12;
- `atoms3u-tv-connector-u002` uses an external M5Stack Unit IR U002 connected
  to the white Grove socket, with IR TX on GPIO 2.

TV profiles, signed commands and host settings remain identical. Firmware
never transmits through both emitters, which prevents a single voice command
from becoming two TV key presses.

## Current validation status

Two deliberately small diagnostic firmware images are provided:

- `atoms3u-hid-diagnostic` contains only the USB keyboard and consumer-control
  interfaces.
- `atoms3u-network-diagnostic` added asynchronous Wi-Fi, an HTTP health endpoint
  and UDP discovery.

Both images compile in CI. That is compile-only evidence: it does not prove USB,
Wi-Fi, discovery, infrared output, or television behavior on physical hardware.
The production image keeps that architecture and adds bounded Sony and LG IR
profiles. A newly flashed profile must pass its matching signed end-to-end
checklist below before it is considered demo-ready.

Bluetooth is **not** part of the production firmware. NimBLE is excluded rather
than merely disabled so the unverified TinyUSB/Bluetooth coexistence path cannot
affect the production image. Wi-Fi is the control link for every output profile.

## Why this USB configuration matters

The production PlatformIO environment uses:

```ini
board = m5stack-atoms3
ARDUINO_USB_MODE=0
ARDUINO_USB_CDC_ON_BOOT=0
```

`ARDUINO_USB_MODE=0` selects the programmable USB-OTG/TinyUSB path. Fixed
hardware CDC/JTAG mode cannot expose the keyboard and consumer-control
interfaces required here. CDC-on-boot is disabled so all HID classes can be
registered before the single `USB.begin()` call.

Startup order is intentional:

1. Register the keyboard and consumer-control HID interfaces.
2. Start the USB device once.
3. Start Wi-Fi without blocking USB startup.
4. Start HTTP and UDP discovery after Wi-Fi receives an IP address.

Wi-Fi reconnects in the main loop. It does not hold USB startup in a long retry
loop.

## Sony Bravia infrared profile

`sony-bravia-ir-v1` maps named remote actions to Sony SIRC frames. It supports
power, Home, Back, arrows, Enter, mute, volume, pause and play. The firmware
sends the initial frame plus two repeats, matching normal Sony remote behavior.
Text entry and arbitrary URLs are deliberately unavailable over IR.

IR is one-way: an `executed` response means the connector emitted the requested
frame, not that the TV confirmed a state change. Position the ATOMS3U where its
emitter can see or reliably reflect toward the television receiver. The board
may be powered by a TV USB port, but it does not need to enumerate as a USB
device under this profile.

The profile includes Sony's model-dependent Netflix command as an experimental
direct app shortcut. Home, Back, discrete power-on/off, Play/Pause and Netflix
can vary by Bravia generation and remain experimental until confirmed on the
exact television. Other apps are rejected rather than approximated with a
brittle app-grid macro.

## LG television infrared profile

`lg-tv-ir-v1` maps named actions to the LG television IR command set. The host
uses NEC address `0x04` and allows these command bytes:

| Action | Command |
| --- | --- |
| Power toggle / experimental on / experimental off | `0x08` / `0xC4` / `0xC5` |
| Home / Back / Input | `0x7C` / `0x28` / `0x0B` |
| Up / Down / Left / Right / Enter | `0x40` / `0x41` / `0x07` / `0x06` / `0x44` |
| Play / Pause | `0xB0` / `0xBA` |
| Mute / Volume up / Volume down | `0x09` / `0x02` / `0x03` |

One-shot keys use an NEC frame plus three protocol repeats at 50% carrier duty.
Repeatable volume and navigation steps remain single-frame so a requested count
stays exact. Every semantic step has a 50 ms trailing gap. The stronger one-shot
burst is used because the AtomS3U's built-in emitter is small. The profile does
not claim a universal
Netflix, YouTube or other app shortcut, and it rejects text search and direct
video selection rather than fabricating success.

LG Power Toggle still requires exact-model physical acceptance testing. The
AtomS3U beam exits through
the tiny opening on the **long side beside the white Grove socket**, not through
the blunt end opposite USB. Aim that side opening at the TV's lower-bezel
receiver. The remaining commands still require the acceptance sequence below.
Home `0x7C` and
discrete power `0xC4`/`0xC5` are especially model-dependent. Like Sony IR, the
transport is one-way and needs optical reach from the ATOMS3U emitter to the TV
receiver.

## LG webOS hybrid profile

`lg-webos-hybrid-v1` is the demo profile for a smart LG TV that enumerates the
ATOMS3U as a USB keyboard. Power, Input, media and volume remain on the LG NEC
infrared path. Home, Back, Search, arrows, Enter and printable ASCII text use
USB HID. This lets one bounded macro open the TV search, type `YouTube`, select
the app, and later type a video id without pretending that LG has a universal
YouTube infrared button.

The profile requires both paths at the same time: the U002 must face the TV's
IR receiver and the ATOMS3U USB plug must be in a data-capable TV port. Health
must report `transport: wifi-to-lg-ir-usb-hid`, `control_ready: true`,
`ir_ready: true` and `usb_ready: true`. USB acceptance remains model-dependent;
an `executed` response proves that reports were sent, while the visible TV UI
is the end-to-end proof.

## Learning a model-specific remote key

The optional U002 receiver on GPIO 1 can be used with the temporary
`atoms3u-ir-learning` firmware environment. Its read-only `/health` response
reports the most recent non-repeat frame, including protocol, address, command,
bit count and raw value. This is the preferred way to calibrate a Home or app
key when a TV ignores the documented generic code. Restore the production
`atoms3u-tv-connector-u002` image after recording the value; the learning image
does not execute signed Room Agent commands.

## Signed command protocol

The connector accepts a small, bounded macro language rather than arbitrary
code:

- HMAC-SHA256 over the timestamp, nonce and exact JSON body.
- 30-second timestamp window, replayed-nonce rejection, and a signed per-boot
  identifier that invalidates captured requests after reboot.
- UUID request id and a named, versioned TV profile.
- Only `key`, printable-ASCII `text`, and bounded `wait` operations.
- At most 32 steps, 120 text characters and 20 seconds of total waits.
- No shell commands, firmware-selected URLs or executable code.

The semantic keys include Home, Back, arrows, Enter, media, mute, volume and
power. The selected profile decides which keys it can execute and rejects an
unsupported operation instead of reporting false success. Whether a TV accepts
every USB or IR command still needs a short acceptance test on the exact model.

## Rehearse with the simulator

From the `room_agent` directory, initialize the ignored connector key once and
run the simulator:

```powershell
..\.venv\Scripts\python.exe configure_connector.py init
..\.venv\Scripts\python.exe connector_simulator.py `
  --key-file .\tv-connector.key
```

If `tv-connector.key` already exists, keep it. Do not regenerate, print or paste
the key.

In another PowerShell window, exercise Windows routing without changing the
installed iPhone service:

```powershell
..\.venv\Scripts\python.exe windows_iphone_host.py `
  --text "Open Netflix on the TV" `
  --tv-connector-url http://127.0.0.1:8780 `
  --tv-connector-key-file .\tv-connector.key
```

Other useful typed rehearsals are:

```text
Open YouTube on the TV
Google weight of an elephant on the TV
Open the first result
Pause
Continue
Close
```

## Prepare the real connector

Copy the ignored firmware settings template once, then provision the 2.4 GHz
Wi-Fi credentials and synchronize the existing connector key without displaying
either secret:

```powershell
Copy-Item `
  .\firmware\atoms3u_tv_connector\include\secrets.example.h `
  .\firmware\atoms3u_tv_connector\include\secrets.h

..\.venv\Scripts\python.exe configure_connector.py provision-firmware `
  --ssid "RoomAgent-Link"
```

Select exactly one output profile before building. This example selects Sony;
use `lg-tv-ir-v1` for LG NEC commands, or `generic-usb-hid-v1` for USB HID.
Every exact TV model still requires the acceptance sequence below:

```powershell
..\.venv\Scripts\python.exe configure_connector.py set-firmware-profile `
  --profile sony-bravia-ir-v1
```

This changes only the profile in the ignored firmware header. It preserves the
Wi-Fi password and connector key.

Build the production environment:

```powershell
..\.venv\Scripts\pio.exe run `
  -d .\firmware\atoms3u_tv_connector `
  -e atoms3u-tv-connector
```

When the Unit IR U002 is physically connected, build and flash its prepared
variant instead:

```powershell
..\.venv\Scripts\pio.exe run `
  -d .\firmware\atoms3u_tv_connector `
  -e atoms3u-tv-connector-u002
```

Unplug power before connecting the keyed Grove cable. Its black/red/yellow/
white conductors map to GND/5 V/GPIO 2 TX/GPIO 1 RX. The receiver is wired but
disabled in this version. After flashing, `/health` must report
`ir_output: grove-unit-ir-u002`, `ir_pin: 2`, and `ir_ready: true`. The U002's
firmware also reports `ir_carrier_duty_percent: 50` and keeps
`ir_receiver_enabled: false`. The official range is up to 5 m, not a guarantee
through a panel or at every angle; validate it from the intended mounting
position.

The two diagnostic environments are available when a regression must be
isolated:

```powershell
..\.venv\Scripts\pio.exe run `
  -d .\firmware\atoms3u_tv_connector `
  -e atoms3u-hid-diagnostic

..\.venv\Scripts\pio.exe run `
  -d .\firmware\atoms3u_tv_connector `
  -e atoms3u-network-diagnostic
```

They are diagnostic images, not the final demo firmware.

## Flashing and normal USB behavior

Enter the ATOMS3U download mode and upload the selected PlatformIO environment
to the temporary serial port. After a successful upload, unplug and reconnect
the board to cold-boot the application.

The serial/COM port used for flashing is expected to disappear after the
application boots because CDC-on-boot is disabled. That is normal. In normal
operation Windows or the TV should instead see stable HID keyboard and consumer
control devices.

These two observations are different:

- **Normal:** one enumeration after a cold boot, followed by stable HID devices;
  the flashing COM port is absent.
- **Failure:** the connector repeatedly appears and disappears every few
  seconds, often accompanied by alternating status flashes. That indicates a
  reset or USB re-enumeration loop and is not an acceptable steady state.

## Discover and select the connector

The connector broadcasts a small JSON discovery record on UDP port `8781`.
The record contains no credential and grants no control; each execution request
still requires the private HMAC key.

```powershell
..\.venv\Scripts\python.exe configure_connector.py discover --timeout 10
```

Select the returned `url` in the host's ignored `.env` file without displaying
or replacing any other secrets:

```powershell
..\.venv\Scripts\python.exe configure_connector.py enable-wifi-host `
  --url http://CONNECTOR_IP:80 `
  --profile sony-bravia-ir-v1
```

Pass `--profile lg-tv-ir-v1` in both host commands when the firmware was built
for LG. Do not mix firmware and host profiles.

This stores the equivalent of the following private configuration and leaves
the Bluetooth name empty so the host selects the authenticated Wi-Fi transport:

```dotenv
TV_CONNECTOR_BLE_NAME=
TV_CONNECTOR_URL=http://CONNECTOR_IP:80
TV_CONNECTOR_KEY_FILE=C:\absolute\path\to\tv-connector.key
TV_CONNECTOR_PROFILE=sony-bravia-ir-v1
```

Verify health with the discovered URL:

```powershell
..\.venv\Scripts\python.exe configure_connector.py status `
  --url http://CONNECTOR_IP:80 `
  --profile sony-bravia-ir-v1
```

The firmware and host profile names must match. A mismatch is rejected before
any remote command is emitted.

The production `/health` response exposes enough information to distinguish a
network problem from a USB problem:

- selected `profile`, `transport` and `control_ready`;
- `ir_available`, `ir_ready`, `ir_send_count` and the last IR key;
- `ir_output`, `ir_pin`, carrier duty and disabled receiver telemetry;
- `usb_driver_started`, `usb_mounted`, `usb_suspended` and `usb_ready`;
- mount, unmount, suspend and resume counts;
- current boot identifier, retained boot count and reset reason;
- uptime, Wi-Fi signal strength and IP address.

For LG, health should report `transport: wifi-to-lg-nec-ir` and
`ir_protocol: lg-nec`. These fields identify the emitted protocol only; they do
not prove that a physical LG television received or accepted a command.

A healthy, provisioned connector should report `status: ok`, `configured: true`
and `control_ready: true`. Generic HID additionally needs `usb_ready: true`.
An IR profile needs `ir_ready: true`; `usb_ready` may remain false because USB
is only supplying power. Execution is rejected when the selected transport is
not ready or the placeholder connector key has not been replaced.

If the corporate network isolates wireless clients, put the Windows host and
connector on a private 2.4 GHz Mobile Hotspot. The optional scoped firewall rule
opens only the discovery port on that hotspot adapter:

```powershell
.\install-connector-discovery-firewall.ps1
```

## Production acceptance checklist

Do not skip directly to an app-opening command. First verify the shared layers:

1. Cold-boot the production firmware and discover it over Wi-Fi.
2. Confirm `/health` names the intended profile and reports
   `control_ready: true`.
3. Wait at least 30 seconds and confirm uptime increases without a new boot.

For `sony-bravia-ir-v1`:

1. Put the ATOMS3U where the IR emitter has line of sight to the receiver.
2. Send Mute twice and verify that audio mutes, then returns.
3. Send Volume Up once, then Home, one arrow and Enter.
4. Verify Pause and Continue use distinct commands during real playback.
5. Try the dedicated Netflix command and record whether this Bravia supports it.
6. Test Power last, after harmless commands prove the optical path.

For `lg-tv-ir-v1` (all exact-model physical acceptance pending):

1. Aim the small side emitter opening beside the white Grove socket at the TV's
   lower-bezel receiver, then confirm `/health` reports the LG profile and
   `ir_ready: true`.
2. Send Mute twice and verify that audio mutes, then returns.
3. Send Volume Up once, then one arrow and Enter in a harmless menu.
4. Verify Home, Back and Input individually from a known state.
5. Verify explicit Pause and Play during real playback.
6. Test Power Toggle last. Treat all power behavior, including discrete
   power-on/off, as experimental until it is observed on the exact LG model
   with an always-powered source and recorded as acceptance evidence.
7. Record the model and every accepted/rejected command before describing the
   LG profile as hardware-validated.

For `generic-usb-hid-v1`:

1. Confirm `/health` reports `usb_mounted: true` and `usb_ready: true`.
2. Send one signed `RIGHT`, then `ENTER`, while a harmless menu is visible.
3. Confirm printable text entry in the TV's search field.
4. Record which Home, media, volume and power usages the exact TV accepts.

USB HID is mostly one-way. Until a native TV API or visual feedback channel is
added, navigation macros should begin from a known state such as Home. Some TVs
remove USB power in standby, and IR is also one-way. Use an always-powered USB
source when standby control matters, and treat the user's visible confirmation
as the hardware acceptance result.

## Hardware references

- [M5Stack ATOMS3U infrared transmitter example](https://docs.m5stack.com/en/arduino/m5atoms3u/ir_nec)
- [M5Stack Unit IR U002](https://docs.m5stack.com/en/unit/ir)
- [Arduino-IRremote Sony SIRC implementation](https://github.com/Arduino-IRremote/Arduino-IRremote/blob/v4.7.1/src/ir_Sony.hpp)
- [Sony Professional BRAVIA remote command reference](https://pro-bravia.sony.net/remote-display-control/ircc-ip/)
- [LG owner manual IR waveform and command table](https://gscs-b2c.lge.com/open/downloadFile?fileId=KROWM000128277.pdf)
- [Arduino-IRremote NEC implementation](https://github.com/Arduino-IRremote/Arduino-IRremote/blob/v4.7.1/src/ir_NEC.hpp)
- [IRDB manufacturer-independent code database](https://github.com/probonopd/irdb)
