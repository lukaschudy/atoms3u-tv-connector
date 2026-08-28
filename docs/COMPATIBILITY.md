# TV compatibility

TV brand names do not guarantee a control protocol. Test the exact model and
record visible results before calling it supported.

## Evidence levels

- **Compiled:** the firmware builds in CI with pinned dependencies.
- **Protocol-tested:** host and firmware schemas, profiles, signing, and bounds
  are covered by automated tests.
- **Simulated:** a signed end-to-end command passed through the virtual TV.
- **Physically validated:** a person saw the exact TV perform the command.

Only the final level proves compatibility with a model.

## Current matrix

| Profile | Transport | Automated evidence | Physical evidence | Important limits |
|---|---|---|---|---|
| `lg-tv-ir-v1` | LG NEC IR | Compiled, protocol-tested, simulated | Pending | Home and discrete power are model-dependent; all keys require exact-model testing |
| `sony-bravia-ir-v1` | Sony SIRC IR | Compiled, protocol-tested, simulated | Pending | Home, Back, discrete power, media, and Netflix vary by generation |
| `generic-usb-hid-v1` | USB keyboard + consumer control | Compiled, protocol-tested, simulated | Pending | Each TV decides which HID usages it accepts; standby may remove USB power |
| `lg-webos-hybrid-v1` | LG IR + USB HID | Compiled, protocol-tested, simulated | Pending | Requires both optical reach and a data-capable TV USB port |

## Safe acceptance sequence

1. Cold-boot the production firmware.
2. Discover it and check `/health` twice, at least 30 seconds apart. Uptime should
   increase without a new boot.
3. Confirm the reported profile and physical emitter match the build.
4. Try **Mute** twice and verify audio returns to its initial state.
5. Try one **Volume Up**, one arrow, and **Enter** in a harmless menu.
6. Test Home, Back, Input, Play, and Pause one at a time.
7. Test power last.

An `executed` response confirms that the one-way output was transmitted. The
visible TV behavior is the acceptance result.

## Report a model

Open a hardware compatibility issue and include:

```text
TV manufacturer and exact model:
Approximate model year / region:
Connector profile:
Emitter: built-in / Unit IR U002 / USB HID
Power source and placement:
Firmware revision:
Health status (remove local IP and credentials):

Mute:
Volume up/down:
Home / Back / Input:
Arrows / Enter:
Play / Pause:
Power toggle / discrete power:
Text entry (USB/hybrid only):
Notes:
```

Do not upload `secrets.h`, `tv-connector.key`, configured firmware binaries, or
screenshots containing Wi-Fi names and local addresses.
