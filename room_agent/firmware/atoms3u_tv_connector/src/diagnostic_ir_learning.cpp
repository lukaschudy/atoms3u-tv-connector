#include <Arduino.h>
#include <ArduinoJson.h>

#define DISABLE_CODE_FOR_SEND
#define NO_LED_RECEIVE_FEEDBACK_CODE
#define IR_RECEIVE_PIN 1
#include <IRremote.hpp>

#include <WebServer.h>
#include <WiFi.h>
#include <WiFiUdp.h>

#include "secrets.h"

namespace {

constexpr uint16_t kDiscoveryPort = 8781;
constexpr uint32_t kDiscoveryIntervalMs = 3000;
constexpr uint32_t kWifiRetryIntervalMs = 5000;

WebServer server(80);
WiFiUDP discoveryUdp;

uint32_t captureCount = 0;
uint32_t lastDiscoveryMs = 0;
uint32_t lastWifiRetryMs = 0;
bool networkServicesStarted = false;
bool hasCapture = false;
decode_type_t lastProtocol = UNKNOWN;
uint16_t lastAddress = 0;
uint16_t lastCommand = 0;
uint16_t lastBits = 0;
uint8_t lastFlags = 0;
IRDecodedRawDataType lastRawData = 0;

String rawHex(IRDecodedRawDataType value) {
  char buffer[2 + (sizeof(IRDecodedRawDataType) * 2) + 1];
  snprintf(buffer, sizeof(buffer), "0x%llX",
           static_cast<unsigned long long>(value));
  return String(buffer);
}

void sendJson(int status, JsonDocument& document) {
  String body;
  serializeJson(document, body);
  server.sendHeader("Cache-Control", "no-store");
  server.send(status, "application/json", body);
}

void handleHealth() {
  JsonDocument document;
  document["version"] = 1;
  document["status"] = "ok";
  document["device"] = ROOM_AGENT_CONNECTOR_NAME;
  document["mode"] = "ir-learning";
  document["receiver_enabled"] = true;
  document["receive_pin"] = IR_RECEIVE_PIN;
  document["capture_count"] = captureCount;
  document["has_capture"] = hasCapture;
  document["wifi_rssi"] = WiFi.RSSI();
  document["ip"] = WiFi.localIP().toString();
  document["uptime_ms"] = millis();
  if (hasCapture) {
    document["protocol"] = getProtocolString(lastProtocol);
    document["protocol_id"] = static_cast<int>(lastProtocol);
    document["address"] = lastAddress;
    document["command"] = lastCommand;
    document["bits"] = lastBits;
    document["flags"] = lastFlags;
    document["raw"] = rawHex(lastRawData);
  }
  sendJson(200, document);
}

void startNetworkServices() {
  if (networkServicesStarted || WiFi.status() != WL_CONNECTED) return;
  server.on("/health", HTTP_GET, handleHealth);
  server.onNotFound([]() {
    JsonDocument document;
    document["status"] = "not_found";
    sendJson(404, document);
  });
  server.begin();
  discoveryUdp.begin(kDiscoveryPort);
  networkServicesStarted = true;
}

void announce() {
  if (!networkServicesStarted ||
      millis() - lastDiscoveryMs < kDiscoveryIntervalMs) return;
  lastDiscoveryMs = millis();
  JsonDocument document;
  document["magic"] = "room-agent-tv-connector";
  document["version"] = 1;
  document["name"] = ROOM_AGENT_CONNECTOR_NAME;
  document["profile"] = "ir-learning-v1";
  document["ip"] = WiFi.localIP().toString();
  document["port"] = 80;
  document["wifi_rssi"] = WiFi.RSSI();
  String payload;
  serializeJson(document, payload);
  discoveryUdp.beginPacket(IPAddress(255, 255, 255, 255), kDiscoveryPort);
  discoveryUdp.print(payload);
  discoveryUdp.endPacket();
}

void captureIr() {
  if (!IrReceiver.decode()) return;
  const IRData data = IrReceiver.decodedIRData;
  IrReceiver.resume();
  if ((data.flags & IRDATA_FLAGS_IS_REPEAT) != 0) return;
  hasCapture = true;
  ++captureCount;
  lastProtocol = data.protocol;
  lastAddress = data.address;
  lastCommand = data.command;
  lastBits = data.numberOfBits;
  lastFlags = data.flags;
  lastRawData = data.decodedRawData;
}

}  // namespace

void setup() {
  IrReceiver.begin(IR_RECEIVE_PIN, DISABLE_LED_FEEDBACK);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(ROOM_AGENT_WIFI_SSID, ROOM_AGENT_WIFI_PASSWORD);
}

void loop() {
  captureIr();

  if (WiFi.status() == WL_CONNECTED) {
    startNetworkServices();
    server.handleClient();
    announce();
  } else if (millis() - lastWifiRetryMs >= kWifiRetryIntervalMs) {
    lastWifiRetryMs = millis();
    WiFi.reconnect();
  }
  delay(1);
}
