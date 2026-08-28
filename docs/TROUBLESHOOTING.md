# Troubleshooting

Run the safe checker first:

```bash
.venv/bin/python room_agent/setup_tv_connector.py doctor
```

On Windows PowerShell, use
`.\.venv\Scripts\python.exe room_agent\setup_tv_connector.py doctor`.

It reports only pass/fix states and never prints the Wi-Fi password or connector
key.

## PlatformIO is not installed

Run the matching repository setup script again:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -SkipPrepare
```

```bash
./scripts/setup.sh --skip-prepare
```

Use the Python executable inside `.venv` for all later setup and flash commands.

## Upload port not found

1. Use a USB-C **data** cable, not a charge-only cable.
2. Put the ATOMS3U in download mode immediately before `flash`.
3. Close serial monitors and other programs using the port.
4. Pass the port explicitly if automatic detection still fails:

```powershell
.\.venv\Scripts\python.exe room_agent\setup_tv_connector.py flash --port COM5
```

The COM/serial port normally disappears after a successful production boot.
That is expected because CDC-on-boot is disabled.

## Connector repeatedly appears and disappears

Repeated USB enumeration usually means a reset loop. Flash the production image
again, cold-boot it, and avoid adding Bluetooth/NimBLE to the TinyUSB production
build. One enumeration followed by stable HID devices is healthy.

## Connector is not discovered

- Confirm the network offers 2.4 GHz Wi-Fi; ATOMS3U does not use 5 GHz.
- Keep the command computer and connector on the same trusted network.
- Check that client isolation or a guest network is not blocking peer traffic.
- Allow inbound UDP port `8781` on the command computer's private-network
  firewall profile.
- Discovery is only a convenience. If the router shows the connector address,
  pass that URL directly to `tv_remote.py`.

## Health works but commands are rejected

The host profile must exactly match the profile compiled into the firmware. The
host key must also be the key synchronized into `secrets.h` before flashing.
Run `prepare`, flash again, and keep the existing generated key unless you are
intentionally rotating it.

## IR command executes but the TV does nothing

- Aim the built-in emitter's tiny side opening beside the white Grove socket at
  the TV's IR receiver. Do not aim the blunt end.
- Move closer and remove obstructions.
- Try the external Unit IR U002 from the intended mounting position.
- Verify the selected TV profile matches the TV platform.
- Test Mute and Volume before model-dependent Home or power keys.
- Remember that the HTTP response confirms transmission, not optical receipt.

## USB HID is not ready

- Use a data-capable TV USB port and cable/adapter path.
- Some TV ports provide power but do not enumerate keyboards.
- Some TVs remove USB power or data in standby.
- Generic HID support is model-specific even when enumeration succeeds.

## U002 health reports the built-in emitter

Flash the explicit U002 environment:

```bash
.venv/bin/python room_agent/setup_tv_connector.py flash --emitter u002
```

On Windows PowerShell, use
`.\.venv\Scripts\python.exe room_agent\setup_tv_connector.py flash --emitter u002`.

Health should report `ir_output: grove-unit-ir-u002` and `ir_pin: 2`. Disconnect
power before attaching or removing the Grove cable.

## Starting over safely

Delete only the ignored `room_agent/tv-connector.key` and firmware
`include/secrets.h`, then run setup again and reflash. This rotates the
authentication key. Never delete or regenerate one of those files without
reflashing, because the host and firmware keys must match.
