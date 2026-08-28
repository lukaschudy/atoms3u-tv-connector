# Future concept: Agent Key

Status: parked until the room voice prototype is reliable.

## Product idea

Separate the voice controller, agent brain, and controlled device. Any authorized
phone, watch, tablet, laptop, or headset can capture a command. A small USB
"Agent Key" physically identifies the target device.

```text
controller with microphone -> Room Agent -> target registered by Agent Key
```

Moving the key to another computer or room changes the active target. Removing
it disables that physical target.

## Roles

- **Controller:** captures voice or button input and submits text plus a target
  identifier.
- **Agent brain:** performs speech recognition, intent planning, capability
  selection, and result reporting. It may run on the Pi or use cloud APIs.
- **Agent Key:** provides a unique identity, pairing, proximity discovery, and
  target capabilities.
- **Executor:** a companion on Windows/Linux/Pi, a native smart-TV integration,
  or the Pi HDMI player.

## Proposed hardware

Prototype with an ESP32-S3 because it provides Wi-Fi, Bluetooth LE, and native
USB device support in one small board.

The production key should expose:

- USB CDC/serial for structured commands and acknowledgements;
- USB HID media/keyboard controls as a driverless fallback;
- BLE advertisement containing an opaque key identifier;
- an RGB LED for unpaired, ready, executing, success, and error states;
- a physical pairing/reset button;
- signed firmware and per-key credentials.

An nRF52840 USB dongle is an alternative for a BLE/Thread/Zigbee-focused key,
but it lacks Wi-Fi. A Zigbee/Thread coordinator is useful for buttons and
sensors, not as the primary Apple Watch or phone transport.

## Target behavior

### PC or Raspberry Pi

The host companion detects the USB serial identity, registers the endpoint and
its capabilities, executes structured actions, and unregisters it when the key
is removed. HID supports simple media and presentation actions when the
companion is unavailable.

### Smart TV

A television USB port is not a generic application-control interface. A key
plugged into it can be powered by USB and advertise room identity, while Room
Agent controls the mapped TV through Android TV, Apple TV, Cast, a vendor API,
or HDMI-CEC.

### Ordinary or inaccessible display

Use the Pi HDMI player. A later universal product could be an HDMI Agent Key,
similar in topology to a streaming stick, with USB used only for power.

## Protocol sketch

Controller request:

```json
{
  "target_key": "agent-key-ab12",
  "text": "Play the latest Cinkatko podcast",
  "request_id": "unique-request-id"
}
```

Endpoint registration:

```json
{
  "target_key": "agent-key-ab12",
  "name": "Prague conference PC",
  "capabilities": [
    "web.open",
    "web.search",
    "media.pause",
    "media.resume",
    "presentation.next"
  ]
}
```

The registry must route only to currently connected endpoints. Execution uses
request IDs for idempotency and returns an explicit success or error result.

## Development sequence

1. Add a target registry to Room Agent.
2. Build a small phone/browser voice controller.
3. Emulate the key with a uniquely identified USB drive detected by the host
   companion.
4. Replace the passive drive with an ESP32-S3 USB serial/HID prototype.
5. Add BLE discovery, pairing, and LED state feedback.
6. Add smart-TV room mappings and capability reporting.
7. Evaluate a compact enclosure and an HDMI version.

## Acceptance criteria

- Inserting a key makes exactly one named target available within two seconds.
- Removing it makes that target unavailable within two seconds.
- The controller visibly confirms the selected target before execution.
- Simple media actions work without a cloud LLM.
- Natural-language actions use the same Room Agent router as voice commands.
- Duplicate requests cannot execute twice.
- A failed native-TV action reports failure or uses an explicitly supported
  fallback; it never reports false success.
