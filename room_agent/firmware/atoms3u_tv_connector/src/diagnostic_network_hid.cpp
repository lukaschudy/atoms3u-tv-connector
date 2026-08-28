#include <Arduino.h>
#include <USB.h>
#include <USBHIDConsumerControl.h>
#include <USBHIDKeyboard.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiUdp.h>

#include "secrets.h"

USBHIDKeyboard diagnosticNetworkKeyboard;
USBHIDConsumerControl diagnosticNetworkConsumer;
WebServer diagnosticServer(80);
WiFiUDP diagnosticDiscovery;

namespace {

constexpr uint16_t kDiscoveryPort = 8781;
constexpr uint32_t kRetryIntervalMs = 5000;
constexpr uint32_t kAnnouncementIntervalMs = 2000;

bool serverStarted = false;
uint32_t lastRetryMs = 0;
uint32_t lastAnnouncementMs = 0;

bool wifiOnline() {
  return WiFi.status() == WL_CONNECTED &&
         WiFi.localIP() != IPAddress(0, 0, 0, 0);
}

void announce() {
  String body = "{\"magic\":\"room-agent-tv-connector\",";
  body += "\"version\":1,\"name\":\"atoms3u-network-test\",";
  body += "\"profile\":\"generic-usb-hid-v1\",\"port\":80}";
  diagnosticDiscovery.beginPacket(IPAddress(255, 255, 255, 255),
                                  kDiscoveryPort);
  diagnosticDiscovery.print(body);
  diagnosticDiscovery.endPacket();
}

}  // namespace

void setup() {
  diagnosticNetworkKeyboard.begin();
  diagnosticNetworkConsumer.begin();
  USB.productName("Room Agent Network Diagnostic");
  USB.manufacturerName("Room Agent");
  USB.begin();

  WiFi.mode(WIFI_STA);
  WiFi.setHostname("atoms3u-network-test");
  WiFi.begin(ROOM_AGENT_WIFI_SSID, ROOM_AGENT_WIFI_PASSWORD);
  lastRetryMs = millis();
}

void loop() {
  const uint32_t now = millis();
  if (wifiOnline()) {
    if (!serverStarted) {
      diagnosticServer.on("/health", HTTP_GET, [] {
        String body = "{\"status\":\"ok\",\"device\":\"atoms3u-network-test\",";
        body += "\"transport\":\"wifi-to-usb-hid\",\"usb_mounted\":";
        body += static_cast<bool>(USB) ? "true}" : "false}";
        diagnosticServer.send(200, "application/json", body);
      });
      diagnosticServer.begin();
      diagnosticDiscovery.begin(kDiscoveryPort);
      serverStarted = true;
      announce();
      lastAnnouncementMs = now;
    }
    diagnosticServer.handleClient();
    if (now - lastAnnouncementMs >= kAnnouncementIntervalMs) {
      announce();
      lastAnnouncementMs = now;
    }
  } else if (now - lastRetryMs >= kRetryIntervalMs) {
    WiFi.reconnect();
    lastRetryMs = now;
  }
  delay(2);
}
