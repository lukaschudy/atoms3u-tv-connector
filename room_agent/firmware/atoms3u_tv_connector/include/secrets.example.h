#pragma once

// Copy this file to secrets.h. Never commit secrets.h.
#define ROOM_AGENT_WIFI_SSID "your-2.4-ghz-wifi"
#define ROOM_AGENT_WIFI_PASSWORD "replace-me"

// Exactly 64 hexadecimal characters. Generate it with:
// python -c "import secrets; print(secrets.token_hex(32))"
#define ROOM_AGENT_CONNECTOR_KEY_HEX \
  "0000000000000000000000000000000000000000000000000000000000000000"

#define ROOM_AGENT_CONNECTOR_NAME "atoms3u-tv-01"
// generic-usb-hid-v1, sony-bravia-ir-v1, lg-tv-ir-v1, or
// lg-webos-hybrid-v1.
// The TV profile is independent from the physical IR output. Build the
// atoms3u-tv-connector-u002 environment to use a Grove Unit IR U002.
#define ROOM_AGENT_CONNECTOR_PROFILE "generic-usb-hid-v1"
