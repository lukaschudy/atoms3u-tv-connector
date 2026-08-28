#include <Arduino.h>
#include <ArduinoJson.h>
#define DISABLE_CODE_FOR_RECEIVER
#define NO_LED_SEND_FEEDBACK_CODE

// Select the physical emitter independently from the TV protocol profile.
// The built-in LED remains the default so existing flashed/build workflows do
// not change. The U002's yellow IR_TX wire maps to GPIO2 on the AtomS3U Grove
// socket. Never drive both outputs for one command: that could double-trigger
// a television positioned within range of both emitters.
#define ROOM_AGENT_IR_OUTPUT_BUILTIN 1
#define ROOM_AGENT_IR_OUTPUT_UNIT_IR_U002 2

#ifndef ROOM_AGENT_IR_OUTPUT
#define ROOM_AGENT_IR_OUTPUT ROOM_AGENT_IR_OUTPUT_BUILTIN
#endif

#if ROOM_AGENT_IR_OUTPUT == ROOM_AGENT_IR_OUTPUT_BUILTIN
#define IR_SEND_PIN 12
#define ROOM_AGENT_IR_OUTPUT_NAME "builtin"
#elif ROOM_AGENT_IR_OUTPUT == ROOM_AGENT_IR_OUTPUT_UNIT_IR_U002
#define IR_SEND_PIN 2
#define ROOM_AGENT_IR_OUTPUT_NAME "grove-unit-ir-u002"
#else
#error "ROOM_AGENT_IR_OUTPUT must select the built-in emitter or Grove Unit IR U002"
#endif

#define IR_SEND_DUTY_CYCLE_PERCENT 50
#include <IRremote.hpp>
#include <USB.h>
#include <USBHIDConsumerControl.h>
#include <USBHIDKeyboard.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_system.h>
#include <mbedtls/md.h>
#include <time.h>

#include "firmware_contract.h"
#include "replay_nonce_cache.h"
#include "response_auth_contract.h"
#include "secrets.h"

namespace {

constexpr int kConnectorProtocolVersion = 2;
constexpr char kResponseAuthCapability[] = "hmac-sha256-v1";
constexpr size_t kMaxBodyBytes = 8192;
constexpr size_t kMaxSteps = 32;
constexpr uint32_t kMaxWaitMs = 5000;
constexpr uint32_t kMaxTotalWaitMs = 20000;
constexpr time_t kMinimumValidEpoch = 1704067200;  // 2024-01-01 UTC
constexpr long kClockSkewSeconds = 30;
constexpr size_t kNonceCacheSize = 64;
constexpr uint16_t kDiscoveryPort = 8781;
constexpr uint32_t kDiscoveryIntervalMs = 3000;
constexpr uint32_t kWifiRetryIntervalMs = 5000;
constexpr uint32_t kHttpRequestTimeoutMs = 1500;
constexpr size_t kMaxHttpLineBytes = 255;
constexpr size_t kMaxHttpHeaders = 24;
constexpr char kGenericUsbProfile[] = "generic-usb-hid-v1";
constexpr char kSonyBraviaIrProfile[] = "sony-bravia-ir-v1";
constexpr char kLgTvIrProfile[] = "lg-tv-ir-v1";
constexpr char kLgWebosHybridProfile[] = "lg-webos-hybrid-v1";
constexpr uint8_t kSonyTvAddress = 0x01;
constexpr uint8_t kSonyExtendedAddress = 0x97;
constexpr int_fast8_t kSonyRepeats = 2;
constexpr uint8_t kLgTvAddress = 0x04;
// The AtomS3U's built-in emitter is small. One-shot keys get a stronger burst,
// while repeatable volume/navigation keys stay single-frame so a count of N
// remains exactly N logical steps and cannot exceed the host timeout.
constexpr int_fast8_t kLgReliableRepeats = 3;
constexpr int_fast8_t kLgStepRepeats = 0;
constexpr uint32_t kLgInterCommandGapMs = 50;

WiFiServer server(80);
WiFiUDP discoveryUdp;
USBHIDKeyboard keyboard;
USBHIDConsumerControl consumer;

ReplayNonceCache<kNonceCacheSize> recentNonces;
char bootId[33] = {};
uint32_t lastDiscoveryMs = 0;
uint32_t lastWifiRetryMs = 0;
bool networkOnline = false;
bool networkServicesStarted = false;
bool usbDriverStarted = false;
bool irInitialized = false;
uint32_t irSendCount = 0;
String lastIrKey;
volatile bool usbMounted = false;
volatile bool usbSuspended = false;
volatile uint32_t usbMountCount = 0;
volatile uint32_t usbUnmountCount = 0;
volatile uint32_t usbSuspendCount = 0;
volatile uint32_t usbResumeCount = 0;
esp_reset_reason_t bootResetReason = ESP_RST_UNKNOWN;
RTC_DATA_ATTR uint32_t retainedBootCount = 0;

void onUsbEvent(void*, esp_event_base_t, int32_t eventId, void*) {
  if (eventId == ARDUINO_USB_STARTED_EVENT) {
    usbMounted = true;
    usbSuspended = false;
    ++usbMountCount;
  } else if (eventId == ARDUINO_USB_STOPPED_EVENT) {
    usbMounted = false;
    usbSuspended = false;
    ++usbUnmountCount;
  } else if (eventId == ARDUINO_USB_SUSPEND_EVENT) {
    usbSuspended = true;
    ++usbSuspendCount;
  } else if (eventId == ARDUINO_USB_RESUME_EVENT) {
    usbSuspended = false;
    ++usbResumeCount;
  }
}

bool usbReady() {
  return static_cast<bool>(USB) && usbMounted && !usbSuspended;
}

bool isGenericUsbProfile() {
  return strcmp(ROOM_AGENT_CONNECTOR_PROFILE, kGenericUsbProfile) == 0;
}

bool isSonyBraviaIrProfile() {
  return strcmp(ROOM_AGENT_CONNECTOR_PROFILE, kSonyBraviaIrProfile) == 0;
}

bool isLgTvIrProfile() {
  return strcmp(ROOM_AGENT_CONNECTOR_PROFILE, kLgTvIrProfile) == 0;
}

bool isLgWebosHybridProfile() {
  return strcmp(ROOM_AGENT_CONNECTOR_PROFILE, kLgWebosHybridProfile) == 0;
}

bool isInfraredProfile() {
  return isSonyBraviaIrProfile() || isLgTvIrProfile() ||
         isLgWebosHybridProfile();
}

bool usesUsbHid() {
  return isGenericUsbProfile() || isLgWebosHybridProfile();
}

bool profileSupported() {
  return isGenericUsbProfile() || isInfraredProfile();
}

bool controlReady() {
  return isLgWebosHybridProfile() ? irInitialized && usbReady() :
         isInfraredProfile() ? irInitialized :
         isGenericUsbProfile() ? usbReady() : false;
}

const char* controlTransport() {
  if (isSonyBraviaIrProfile()) return "wifi-to-sony-ir";
  if (isLgTvIrProfile()) return "wifi-to-lg-nec-ir";
  if (isLgWebosHybridProfile()) return "wifi-to-lg-ir-usb-hid";
  if (isGenericUsbProfile()) return "wifi-to-usb-hid";
  return "unsupported";
}

const char* controlUnavailableError() {
  if (!profileSupported()) return "unsupported_profile";
  if (isLgWebosHybridProfile()) {
    if (!irInitialized) return "ir_not_ready";
    return usbSuspended ? "usb_suspended" : "usb_not_mounted";
  }
  if (isInfraredProfile()) return "ir_not_ready";
  return usbSuspended ? "usb_suspended" : "usb_not_mounted";
}

bool isHex(const String& value, size_t expectedLength) {
  if (value.length() != expectedLength) return false;
  for (char character : value) {
    if (!isxdigit(static_cast<unsigned char>(character))) return false;
  }
  return true;
}

bool decodeKey(uint8_t* output) {
  const String encoded = ROOM_AGENT_CONNECTOR_KEY_HEX;
  if (!isHex(encoded, 64)) return false;
  bool allZero = true;
  for (size_t index = 0; index < 32; ++index) {
    const String byteText = encoded.substring(index * 2, index * 2 + 2);
    output[index] = static_cast<uint8_t>(strtoul(byteText.c_str(), nullptr, 16));
    allZero = allZero && output[index] == 0;
  }
  return !allZero;
}

bool connectorKeyConfigured() {
  uint8_t key[32];
  return decodeKey(key);
}

String hmacSha256(const String& message) {
  uint8_t key[32];
  uint8_t digest[32];
  if (!decodeKey(key)) return "";
  const mbedtls_md_info_t* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (info == nullptr ||
      mbedtls_md_hmac(info, key, sizeof(key),
                      reinterpret_cast<const uint8_t*>(message.c_str()),
                      message.length(), digest) != 0) {
    return "";
  }
  char encoded[65];
  for (size_t index = 0; index < sizeof(digest); ++index) {
    snprintf(encoded + index * 2, 3, "%02x", digest[index]);
  }
  encoded[64] = '\0';
  return String(encoded);
}

String hmacResponseBody(const String& body) {
  uint8_t key[32];
  uint8_t digest[32];
  if (!decodeKey(key)) return "";
  const mbedtls_md_info_t* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (info == nullptr) return "";

  mbedtls_md_context_t context;
  mbedtls_md_init(&context);
  int result = mbedtls_md_setup(&context, info, 1);
  if (result == 0) result = mbedtls_md_hmac_starts(&context, key, sizeof(key));
  if (result == 0) {
    result = updateResponseAuthMessage(
        reinterpret_cast<const uint8_t*>(body.c_str()), body.length(),
        [&context](const uint8_t* chunk, size_t size) {
          return mbedtls_md_hmac_update(&context, chunk, size);
        });
  }
  if (result == 0) result = mbedtls_md_hmac_finish(&context, digest);
  mbedtls_md_free(&context);
  if (result != 0) return "";

  char encoded[65];
  for (size_t index = 0; index < sizeof(digest); ++index) {
    snprintf(encoded + index * 2, 3, "%02x", digest[index]);
  }
  encoded[64] = '\0';
  return String(encoded);
}

bool constantTimeEqual(const String& left, const String& right) {
  if (left.length() != right.length()) return false;
  uint8_t difference = 0;
  for (size_t index = 0; index < left.length(); ++index) {
    difference |= static_cast<uint8_t>(left[index] ^ right[index]);
  }
  return difference == 0;
}

struct VerifiedHttpRequest {
  String nonce;
  time_t expiry = 0;
  time_t verifiedAt = 0;
};

struct HttpRequestHeaders {
  String timestamp;
  String nonce;
  String signature;
  String contentType;
  size_t contentLength = 0;
  bool hasContentLength = false;
  bool hasTransferEncoding = false;
};

bool verifyHttpAuthentication(
    const String& body, const HttpRequestHeaders& headers,
    VerifiedHttpRequest& verified, String& error) {
  const String& timestampText = headers.timestamp;
  const String& nonce = headers.nonce;
  const String& supplied = headers.signature;
  const time_t now = time(nullptr);
  size_t parsedTimestamp = 0;
  if (!parseContractContentLength(
          timestampText.c_str(), SIZE_MAX, parsedTimestamp)) {
    error = "invalid_timestamp";
    return false;
  }
  const time_t timestamp = static_cast<time_t>(parsedTimestamp);
  if (timestamp < kMinimumValidEpoch) {
    error = "timestamp_outside_window";
    return false;
  }
  if (!isHex(nonce, 32)) {
    error = "invalid_nonce";
    return false;
  }
  if (!isHex(supplied, 64)) {
    error = "invalid_signature";
    return false;
  }
  const String expected = hmacSha256(timestampText + "\n" + nonce + "\n" + body);
  if (expected.isEmpty() || !constantTimeEqual(expected, supplied)) {
    error = "invalid_signature";
    return false;
  }
  if (now < kMinimumValidEpoch) {
    error = "clock_not_synchronized";
    return false;
  }
  if (labs(static_cast<long>(now - timestamp)) > kClockSkewSeconds) {
    error = "timestamp_outside_window";
    return false;
  }
  verified.nonce = nonce;
  verified.expiry = timestamp + kClockSkewSeconds;
  verified.verifiedAt = now;
  return true;
}

bool consumeVerifiedNonce(const VerifiedHttpRequest& verified, String& error) {
  const ReplayNonceResult nonceResult = recentNonces.remember(
      verified.nonce.c_str(), verified.expiry, verified.verifiedAt);
  if (nonceResult == ReplayNonceResult::Replayed) {
    error = "replayed_nonce";
    return false;
  }
  if (nonceResult == ReplayNonceResult::Full) {
    error = "nonce_cache_full";
    return false;
  }
  return true;
}

const char* httpStatusText(int status) {
  if (status == 200) return "OK";
  if (status == 400) return "Bad Request";
  if (status == 403) return "Forbidden";
  if (status == 404) return "Not Found";
  if (status == 405) return "Method Not Allowed";
  if (status == 408) return "Request Timeout";
  if (status == 411) return "Length Required";
  if (status == 413) return "Content Too Large";
  if (status == 415) return "Unsupported Media Type";
  if (status == 429) return "Too Many Requests";
  if (status == 503) return "Service Unavailable";
  return "Error";
}

void sendJson(
    WiFiClient& client, int status, JsonDocument& document,
    bool authenticate = false) {
  String body;
  serializeJson(document, body);
  const String newline = String(static_cast<char>(13)) + static_cast<char>(10);
  String response = "HTTP/1.1 " + String(status) + " " + httpStatusText(status) +
                    newline + "Content-Type: application/json" + newline +
                    "Cache-Control: no-store" + newline + "Connection: close" +
                    newline;
  if (authenticate) {
    const String signature = hmacResponseBody(body);
    response += String(kResponseSignatureHeader) + ": " + signature + newline;
  }
  response += "Content-Length: " + String(body.length()) + newline + newline;
  client.write(reinterpret_cast<const uint8_t*>(response.c_str()), response.length());
  client.write(reinterpret_cast<const uint8_t*>(body.c_str()), body.length());
}

void sendError(WiFiClient& client, int status, const char* error) {
  JsonDocument document;
  document["status"] = "rejected";
  document["error"] = error;
  sendJson(client, status, document);
}

bool tapKeyboard(uint8_t key) {
  if (keyboard.press(key) == 0) return false;
  delay(40);
  return keyboard.release(key) != 0;
}

bool tapConsumer(uint16_t key) {
  if (consumer.press(key) == 0) return false;
  delay(40);
  return consumer.release() != 0;
}

bool executeUsbKey(const String& key) {
  if (key == "POWER") return tapConsumer(CONSUMER_CONTROL_POWER);
  if (key == "HOME") return tapConsumer(CONSUMER_CONTROL_HOME);
  if (key == "BACK") return tapConsumer(CONSUMER_CONTROL_BACK);
  if (key == "SEARCH") return tapConsumer(CONSUMER_CONTROL_SEARCH);
  if (key == "PLAY_PAUSE") return tapConsumer(CONSUMER_CONTROL_PLAY_PAUSE);
  if (key == "MUTE") return tapConsumer(CONSUMER_CONTROL_MUTE);
  if (key == "VOLUME_UP") {
    return tapConsumer(CONSUMER_CONTROL_VOLUME_INCREMENT);
  }
  if (key == "VOLUME_DOWN") {
    return tapConsumer(CONSUMER_CONTROL_VOLUME_DECREMENT);
  }
  if (key == "UP") return tapKeyboard(KEY_UP_ARROW);
  if (key == "DOWN") return tapKeyboard(KEY_DOWN_ARROW);
  if (key == "LEFT") return tapKeyboard(KEY_LEFT_ARROW);
  if (key == "RIGHT") return tapKeyboard(KEY_RIGHT_ARROW);
  if (key == "ENTER") return tapKeyboard(KEY_RETURN);
  return false;
}

bool supportedUsbKey(const String& key) {
  return key == "POWER" || key == "HOME" || key == "BACK" ||
         key == "SEARCH" || key == "PLAY_PAUSE" || key == "MUTE" ||
         key == "VOLUME_UP" || key == "VOLUME_DOWN" || key == "UP" ||
         key == "DOWN" || key == "LEFT" || key == "RIGHT" ||
         key == "ENTER";
}

bool sendSony(uint16_t address, uint16_t command, uint8_t bits,
              const String& key) {
  if (!irInitialized) return false;
  IrSender.sendSony(address, command, kSonyRepeats, bits);
  ++irSendCount;
  lastIrKey = key;
  return true;
}

bool executeSonyKey(const String& key) {
  if (key == "POWER") {
    return sendSony(kSonyTvAddress, 0x15, SIRCS_12_PROTOCOL, key);
  }
  if (key == "POWER_ON") {
    return sendSony(kSonyTvAddress, 0x2E, SIRCS_12_PROTOCOL, key);
  }
  if (key == "POWER_OFF") {
    return sendSony(kSonyTvAddress, 0x2F, SIRCS_12_PROTOCOL, key);
  }
  if (key == "HOME") {
    return sendSony(kSonyTvAddress, 0x60, SIRCS_12_PROTOCOL, key);
  }
  if (key == "BACK") {
    return sendSony(kSonyExtendedAddress, 0x23, SIRCS_15_PROTOCOL, key);
  }
  if (key == "PLAY") {
    return sendSony(kSonyExtendedAddress, 0x1A, SIRCS_15_PROTOCOL, key);
  }
  if (key == "PAUSE") {
    return sendSony(kSonyExtendedAddress, 0x19, SIRCS_15_PROTOCOL, key);
  }
  if (key == "MUTE") {
    return sendSony(kSonyTvAddress, 0x14, SIRCS_12_PROTOCOL, key);
  }
  if (key == "VOLUME_UP") {
    return sendSony(kSonyTvAddress, 0x12, SIRCS_12_PROTOCOL, key);
  }
  if (key == "VOLUME_DOWN") {
    return sendSony(kSonyTvAddress, 0x13, SIRCS_12_PROTOCOL, key);
  }
  if (key == "UP") {
    return sendSony(kSonyTvAddress, 0x74, SIRCS_12_PROTOCOL, key);
  }
  if (key == "DOWN") {
    return sendSony(kSonyTvAddress, 0x75, SIRCS_12_PROTOCOL, key);
  }
  if (key == "LEFT") {
    return sendSony(kSonyTvAddress, 0x34, SIRCS_12_PROTOCOL, key);
  }
  if (key == "RIGHT") {
    return sendSony(kSonyTvAddress, 0x33, SIRCS_12_PROTOCOL, key);
  }
  if (key == "ENTER") {
    return sendSony(kSonyTvAddress, 0x65, SIRCS_12_PROTOCOL, key);
  }
  if (key == "INPUT") {
    return sendSony(kSonyTvAddress, 0x25, SIRCS_12_PROTOCOL, key);
  }
  if (key == "NETFLIX") {
    return sendSony(0x1A, 0x7C, SIRCS_15_PROTOCOL, key);
  }
  return false;
}

bool supportedSonyKey(const String& key) {
  return key == "POWER" || key == "POWER_ON" || key == "POWER_OFF" ||
         key == "HOME" || key == "BACK" ||
         key == "PLAY" || key == "PAUSE" || key == "MUTE" ||
         key == "VOLUME_UP" || key == "VOLUME_DOWN" || key == "UP" ||
         key == "DOWN" || key == "LEFT" || key == "RIGHT" ||
         key == "ENTER" || key == "INPUT" || key == "NETFLIX";
}

bool sendLg(uint16_t command, const String& key, int_fast8_t repeats) {
  if (!irInitialized) return false;
  // Consumer LG TVs use NEC address 0x04. The familiar published 32-bit
  // values (for example 0x20DF10EF for Power) are MSB-first renderings of
  // this address/command pair and must not be passed as modern raw data.
  IrSender.sendNEC(kLgTvAddress, command, repeats);
  // IRremote intentionally leaves no trailing frame-period delay. A short
  // gap keeps adjacent semantic steps (such as Volume Up x20) distinct.
  delay(kLgInterCommandGapMs);
  ++irSendCount;
  lastIrKey = key;
  return true;
}

bool executeLgKey(const String& key) {
  if (key == "POWER") return sendLg(0x08, key, kLgReliableRepeats);
  if (key == "POWER_ON") return sendLg(0xC4, key, kLgReliableRepeats);
  if (key == "POWER_OFF") return sendLg(0xC5, key, kLgReliableRepeats);
  if (key == "HOME") return sendLg(0x7C, key, kLgReliableRepeats);
  if (key == "BACK") return sendLg(0x28, key, kLgReliableRepeats);
  if (key == "PLAY") return sendLg(0xB0, key, kLgReliableRepeats);
  if (key == "PAUSE") return sendLg(0xBA, key, kLgReliableRepeats);
  if (key == "MUTE") return sendLg(0x09, key, kLgReliableRepeats);
  if (key == "VOLUME_UP") return sendLg(0x02, key, kLgStepRepeats);
  if (key == "VOLUME_DOWN") return sendLg(0x03, key, kLgStepRepeats);
  if (key == "UP") return sendLg(0x40, key, kLgStepRepeats);
  if (key == "DOWN") return sendLg(0x41, key, kLgStepRepeats);
  if (key == "LEFT") return sendLg(0x07, key, kLgStepRepeats);
  if (key == "RIGHT") return sendLg(0x06, key, kLgStepRepeats);
  if (key == "ENTER") return sendLg(0x44, key, kLgReliableRepeats);
  if (key == "INPUT") return sendLg(0x0B, key, kLgReliableRepeats);
  return false;
}

bool supportedLgKey(const String& key) {
  return key == "POWER" || key == "POWER_ON" || key == "POWER_OFF" ||
         key == "HOME" || key == "BACK" || key == "PLAY" ||
         key == "PAUSE" || key == "MUTE" || key == "VOLUME_UP" ||
         key == "VOLUME_DOWN" || key == "UP" || key == "DOWN" ||
         key == "LEFT" || key == "RIGHT" || key == "ENTER" ||
         key == "INPUT";
}

bool supportedLgHybridKey(const String& key) {
  return supportedLgKey(key) || key == "SEARCH";
}

bool lgHybridKeyUsesUsb(const String& key) {
  return key == "HOME" || key == "BACK" || key == "SEARCH" ||
         key == "UP" || key == "DOWN" || key == "LEFT" ||
         key == "RIGHT" || key == "ENTER";
}

bool executeLgHybridKey(const String& key) {
  return lgHybridKeyUsesUsb(key) ? executeUsbKey(key) : executeLgKey(key);
}

bool supportedKey(const String& key) {
  return isSonyBraviaIrProfile() ? supportedSonyKey(key) :
         isLgTvIrProfile() ? supportedLgKey(key) :
         isLgWebosHybridProfile() ? supportedLgHybridKey(key) :
         isGenericUsbProfile() ? supportedUsbKey(key) : false;
}

bool executeKey(const String& key) {
  return isSonyBraviaIrProfile() ? executeSonyKey(key) :
         isLgTvIrProfile() ? executeLgKey(key) :
         isLgWebosHybridProfile() ? executeLgHybridKey(key) :
         isGenericUsbProfile() ? executeUsbKey(key) : false;
}

bool keyUsesUsb(const String& key) {
  return isGenericUsbProfile() ||
         (isLgWebosHybridProfile() && lgHybridKeyUsesUsb(key));
}

void releaseUsbControls() {
  if (!usesUsbHid()) return;
  keyboard.releaseAll();
  consumer.release();
}

void handleHealth(WiFiClient& client) {
  const bool mounted = static_cast<bool>(USB) && usbMounted;
  const bool ready = controlReady();
  const bool configured = connectorKeyConfigured();
  JsonDocument document;
  document["version"] = kConnectorProtocolVersion;
  document["response_auth"] = kResponseAuthCapability;
  document["boot_id"] = bootId;
  document["status"] = (!configured || !profileSupported()) ? "config_error" :
                       ready ? "ok" : "starting";
  document["device"] = ROOM_AGENT_CONNECTOR_NAME;
  document["profile"] = ROOM_AGENT_CONNECTOR_PROFILE;
  document["transport"] = controlTransport();
  document["configured"] = configured;
  document["control_ready"] = ready;
  document["ir_available"] = true;
  document["ir_ready"] = irInitialized;
  document["ir_output"] = ROOM_AGENT_IR_OUTPUT_NAME;
  document["ir_pin"] = IR_SEND_PIN;
  document["ir_carrier_duty_percent"] = IR_SEND_DUTY_CYCLE_PERCENT;
#if ROOM_AGENT_IR_OUTPUT == ROOM_AGENT_IR_OUTPUT_UNIT_IR_U002
  document["ir_receiver_available"] = true;
  document["ir_receive_pin"] = 1;
#else
  document["ir_receiver_available"] = false;
#endif
  document["ir_receiver_enabled"] = false;
  document["ir_protocol"] = isSonyBraviaIrProfile() ? "sony-sirc" :
                            (isLgTvIrProfile() || isLgWebosHybridProfile()) ?
                            "lg-nec" : "none";
  document["ir_send_count"] = irSendCount;
  if (!lastIrKey.isEmpty()) document["last_ir_key"] = lastIrKey;
  document["usb_driver_started"] = usbDriverStarted;
  document["usb_mounted"] = mounted;
  document["usb_suspended"] = usbSuspended;
  document["usb_ready"] = usbReady();
  document["usb_mount_count"] = usbMountCount;
  document["usb_unmount_count"] = usbUnmountCount;
  document["usb_suspend_count"] = usbSuspendCount;
  document["usb_resume_count"] = usbResumeCount;
  document["boot_count"] = retainedBootCount;
  document["reset_reason"] = static_cast<int>(bootResetReason);
  document["uptime_ms"] = millis();
  document["wifi_rssi"] = WiFi.RSSI();
  document["ip"] = WiFi.localIP().toString();
  sendJson(client, 200, document);
}

bool validatePlan(const String& body, JsonDocument& document, String& error) {
  if (body.isEmpty() || body.length() > kMaxBodyBytes) {
    error = "invalid_body_size";
    return false;
  }
  if (deserializeJson(document, body) != DeserializationError::Ok ||
      document.size() != 6 || !document["version"].is<int>() ||
      document["version"].as<int>() != kConnectorProtocolVersion ||
      !document["request_id"].is<const char*>() ||
      !document["boot_id"].is<const char*>() ||
      !document["profile"].is<const char*>() ||
      !document["purpose"].is<const char*>() ||
      !document["steps"].is<JsonArray>()) {
    error = "invalid_schema";
    return false;
  }
  const JsonString requestId = document["request_id"].as<JsonString>();
  const JsonString suppliedBootId = document["boot_id"].as<JsonString>();
  const JsonString profile = document["profile"].as<JsonString>();
  const JsonString purpose = document["purpose"].as<JsonString>();
  if (!validContractUuid(requestId.c_str(), requestId.size()) ||
      !contractBootIdMatches(
          suppliedBootId.c_str(), suppliedBootId.size(), bootId) ||
      !contractBytesEqual(
          profile.c_str(), profile.size(), ROOM_AGENT_CONNECTOR_PROFILE) ||
      !validContractName(purpose.c_str(), purpose.size())) {
    error = "invalid_schema";
    return false;
  }
  JsonArray steps = document["steps"].as<JsonArray>();
  if (steps.size() == 0 || steps.size() > kMaxSteps) {
    error = "invalid_step_count";
    return false;
  }

  uint32_t totalWait = 0;
  for (JsonVariant stepValue : steps) {
    if (!stepValue.is<JsonObject>()) {
      error = "invalid_step";
      return false;
    }
    JsonObject step = stepValue.as<JsonObject>();
    if (!step["op"].is<const char*>()) {
      error = "invalid_step";
      return false;
    }
    const JsonString op = step["op"].as<JsonString>();
    if (contractBytesEqual(op.c_str(), op.size(), "wait")) {
      const int milliseconds = step["ms"] | -1;
      if (step.size() != 2 ||
          !validContractWait(step["ms"].is<int>(), milliseconds)) {
        error = "invalid_wait";
        return false;
      }
      totalWait += static_cast<uint32_t>(milliseconds);
    } else if (contractBytesEqual(op.c_str(), op.size(), "key")) {
      if (step.size() != 2 || !step["key"].is<const char*>()) {
        error = "unsupported_key";
        return false;
      }
      const JsonString keyValue = step["key"].as<JsonString>();
      if (!validContractPrintableAscii(
              keyValue.c_str(), keyValue.size(), 64) ||
          !supportedKey(String(keyValue.c_str(), keyValue.size()))) {
        error = "unsupported_key";
        return false;
      }
    } else if (contractBytesEqual(op.c_str(), op.size(), "text")) {
      if (!usesUsbHid()) {
        error = "unsupported_operation_for_profile";
        return false;
      }
      if (step.size() != 2 || !step["text"].is<const char*>()) {
        error = "invalid_text";
        return false;
      }
      const JsonString textValue = step["text"].as<JsonString>();
      if (!validContractPrintableAscii(
              textValue.c_str(), textValue.size(), 120)) {
        error = "invalid_text";
        return false;
      }
    } else {
      error = "unsupported_operation";
      return false;
    }
  }
  if (totalWait > kMaxTotalWaitMs) {
    error = "total_wait_too_long";
    return false;
  }
  return true;
}

bool executeValidatedPlan(
    JsonDocument& document, JsonDocument& response, String& error) {
  if (!controlReady()) {
    error = controlUnavailableError();
    return false;
  }
  JsonArray steps = document["steps"].as<JsonArray>();

  for (JsonVariant stepValue : steps) {
    if (!controlReady()) {
      error = controlUnavailableError();
      releaseUsbControls();
      return false;
    }
    JsonObject step = stepValue.as<JsonObject>();
    const String op = step["op"] | "";
    if (op == "wait") delay(step["ms"].as<uint32_t>());
    else if (op == "key") {
      if (!executeKey(step["key"].as<String>())) {
        error = keyUsesUsb(step["key"].as<String>()) ?
                "usb_report_failed" : "ir_send_failed";
        releaseUsbControls();
        return false;
      }
    } else if (op == "text") {
      const String text = step["text"].as<String>();
      if (keyboard.print(text) != text.length()) {
        error = "usb_report_failed";
        releaseUsbControls();
        return false;
      }
    }
  }
  if (!controlReady()) {
    error = controlUnavailableError();
    releaseUsbControls();
    return false;
  }
  releaseUsbControls();

  response["version"] = kConnectorProtocolVersion;
  response["status"] = "executed";
  response["request_id"] = document["request_id"];
  response["profile"] = ROOM_AGENT_CONNECTOR_PROFILE;
  response["purpose"] = document["purpose"];
  response["steps_executed"] = steps.size();
  response["transport"] = controlTransport();
  return true;
}

void handleExecute(
    WiFiClient& client, const HttpRequestHeaders& headers, const String& body) {
  if (!connectorKeyConfigured()) {
    sendError(client, 503, "connector_not_configured");
    return;
  }
  String authError;
  VerifiedHttpRequest verified;
  if (!verifyHttpAuthentication(body, headers, verified, authError)) {
    sendError(client, 403, authError.c_str());
    return;
  }
  JsonDocument plan;
  String error;
  if (!validatePlan(body, plan, error)) {
    sendError(client, error == "invalid_body_size" ? 413 : 400, error.c_str());
    return;
  }
  if (!consumeVerifiedNonce(verified, authError)) {
    sendError(
        client, authError == "nonce_cache_full" ? 429 : 403,
        authError.c_str());
    return;
  }
  JsonDocument response;
  if (!executeValidatedPlan(plan, response, error)) {
    int status = 400;
    if (error == "invalid_body_size") status = 413;
    else if (error == "usb_not_mounted" || error == "usb_suspended" ||
             error == "usb_report_failed" || error == "ir_not_ready" ||
             error == "ir_send_failed") status = 503;
    sendError(client, status, error.c_str());
    return;
  }
  sendJson(client, 200, response, true);
}

bool readBoundedHttpLine(
    WiFiClient& client, char* output, size_t capacity,
    uint32_t requestStarted) {
  size_t length = 0;
  while (client.connected() &&
         millis() - requestStarted <= kHttpRequestTimeoutMs) {
    while (client.available() > 0) {
      const int next = client.read();
      if (next < 0) break;
      if (next == '\n') {
        if (length == 0 || output[length - 1] != static_cast<char>(13)) {
          return false;
        }
        --length;
        output[length] = '\0';
        return true;
      }
      if (length > 0 && output[length - 1] == static_cast<char>(13)) {
        return false;
      }
      if (!validContractHttpLineOctet(next)) return false;
      if (length + 1 >= capacity) return false;
      output[length++] = static_cast<char>(next);
    }
    delay(1);
  }
  return false;
}

bool assignUniqueHeader(String& destination, const String& value) {
  if (!destination.isEmpty() || value.isEmpty()) return false;
  destination = value;
  return true;
}

bool parseHttpHeaders(
    WiFiClient& client, HttpRequestHeaders& headers, int& errorStatus,
    uint32_t requestStarted) {
  char line[kMaxHttpLineBytes + 1];
  for (size_t count = 0; count < kMaxHttpHeaders; ++count) {
    if (!readBoundedHttpLine(client, line, sizeof(line), requestStarted)) {
      errorStatus = 408;
      return false;
    }
    if (line[0] == '\0') return true;
    char* separator = strchr(line, ':');
    if (separator == nullptr || separator == line) {
      errorStatus = 400;
      return false;
    }
    *separator = '\0';
    char* valueStart = separator + 1;
    while (*valueStart == ' ' || *valueStart == '\t') ++valueStart;
    char* valueEnd = valueStart + strlen(valueStart);
    while (valueEnd > valueStart &&
           (valueEnd[-1] == ' ' || valueEnd[-1] == '\t')) {
      --valueEnd;
    }
    *valueEnd = '\0';
    const String name(line);
    const String value(valueStart);
    bool accepted = true;
    if (name.equalsIgnoreCase("Content-Length")) {
      if (headers.hasContentLength) {
        accepted = false;
      } else {
        size_t parsed = 0;
        if (!parseContractContentLength(value.c_str(), SIZE_MAX, parsed)) {
          accepted = false;
        } else if (parsed > kMaxBodyBytes) {
          errorStatus = 413;
          return false;
        } else {
          headers.contentLength = parsed;
          headers.hasContentLength = true;
        }
      }
    } else if (name.equalsIgnoreCase("Content-Type")) {
      accepted = assignUniqueHeader(headers.contentType, value);
    } else if (name.equalsIgnoreCase("Transfer-Encoding")) {
      headers.hasTransferEncoding = true;
    } else if (name.equalsIgnoreCase("X-Room-Agent-Timestamp")) {
      accepted = assignUniqueHeader(headers.timestamp, value);
    } else if (name.equalsIgnoreCase("X-Room-Agent-Nonce")) {
      accepted = assignUniqueHeader(headers.nonce, value);
    } else if (name.equalsIgnoreCase("X-Room-Agent-Signature")) {
      accepted = assignUniqueHeader(headers.signature, value);
    }
    if (!accepted) {
      errorStatus = 400;
      return false;
    }
  }
  errorStatus = 400;
  return false;
}

bool readBoundedHttpBody(
    WiFiClient& client, size_t expectedLength, String& body,
    uint32_t requestStarted) {
  if (!body.reserve(expectedLength)) return false;
  uint8_t chunk[512];
  size_t received = 0;
  while (received < expectedLength &&
         client.connected() &&
         millis() - requestStarted <= kHttpRequestTimeoutMs) {
    const int available = client.available();
    if (available <= 0) {
      delay(1);
      continue;
    }
    const size_t remaining = expectedLength - received;
    const size_t wanted = min(
        remaining, min(sizeof(chunk), static_cast<size_t>(available)));
    const int count = client.read(chunk, wanted);
    if (count <= 0) continue;
    if (!body.concat(reinterpret_cast<const char*>(chunk), count)) return false;
    received += static_cast<size_t>(count);
  }
  return received == expectedLength && body.length() == expectedLength;
}

void handleHttpClient() {
  WiFiClient client = server.available();
  if (!client) return;
  const uint32_t requestStarted = millis();
  char requestLine[kMaxHttpLineBytes + 1];
  if (!readBoundedHttpLine(
          client, requestLine, sizeof(requestLine), requestStarted)) {
    client.stop();
    return;
  }
  char method[8] = {};
  char path[96] = {};
  char version[16] = {};
  char extra = '\0';
  if (sscanf(requestLine, "%7s %95s %15s %c", method, path, version, &extra) != 3 ||
      (strcmp(version, "HTTP/1.1") != 0 && strcmp(version, "HTTP/1.0") != 0)) {
    sendError(client, 400, "invalid_request_line");
    client.stop();
    return;
  }
  HttpRequestHeaders headers;
  int errorStatus = 400;
  if (!parseHttpHeaders(client, headers, errorStatus, requestStarted)) {
    sendError(client, errorStatus, "invalid_headers");
    client.stop();
    return;
  }
  if (strcmp(method, "GET") == 0 && strcmp(path, "/health") == 0) {
    if (headers.hasTransferEncoding ||
        (headers.hasContentLength && headers.contentLength != 0)) {
      sendError(client, 400, "unexpected_body");
    } else {
      handleHealth(client);
    }
    client.stop();
    return;
  }
  if (strcmp(method, "POST") != 0 || strcmp(path, "/v1/execute") != 0) {
    sendError(
        client,
        strcmp(path, "/health") == 0 || strcmp(path, "/v1/execute") == 0 ?
            405 : 404,
        strcmp(path, "/health") == 0 || strcmp(path, "/v1/execute") == 0 ?
            "method_not_allowed" : "not_found");
    client.stop();
    return;
  }
  if (headers.hasTransferEncoding) {
    sendError(client, 400, "transfer_encoding_not_supported");
  } else if (!headers.hasContentLength) {
    sendError(client, 411, "content_length_required");
  } else if (!headers.contentType.equalsIgnoreCase("application/json")) {
    sendError(client, 415, "content_type_not_supported");
  } else {
    String body;
    if (!readBoundedHttpBody(
            client, headers.contentLength, body, requestStarted)) {
      sendError(client, 408, "incomplete_body");
    } else {
      handleExecute(client, headers, body);
    }
  }
  client.stop();
}

bool wifiHasAddress() {
  return WiFi.status() == WL_CONNECTED &&
         WiFi.localIP() != IPAddress(0, 0, 0, 0);
}

IPAddress subnetBroadcastAddress() {
  const IPAddress address = WiFi.localIP();
  const IPAddress mask = WiFi.subnetMask();
  return IPAddress(
      address[0] | static_cast<uint8_t>(~mask[0]),
      address[1] | static_cast<uint8_t>(~mask[1]),
      address[2] | static_cast<uint8_t>(~mask[2]),
      address[3] | static_cast<uint8_t>(~mask[3]));
}

void sendDiscoveryPacket(const IPAddress& destination, const String& body) {
  discoveryUdp.beginPacket(destination, kDiscoveryPort);
  discoveryUdp.write(reinterpret_cast<const uint8_t*>(body.c_str()), body.length());
  discoveryUdp.endPacket();
}

void announceDiscovery() {
  JsonDocument document;
  document["magic"] = "room-agent-tv-connector";
  document["version"] = 1;
  document["name"] = ROOM_AGENT_CONNECTOR_NAME;
  document["profile"] = ROOM_AGENT_CONNECTOR_PROFILE;
  document["ip"] = WiFi.localIP().toString();
  document["port"] = 80;
  document["wifi_rssi"] = WiFi.RSSI();
  String body;
  serializeJson(document, body);
  sendDiscoveryPacket(IPAddress(255, 255, 255, 255), body);
  const IPAddress subnetBroadcast = subnetBroadcastAddress();
  if (subnetBroadcast != IPAddress(255, 255, 255, 255)) {
    sendDiscoveryPacket(subnetBroadcast, body);
  }
}

void updateNetworkState() {
  const bool connected = wifiHasAddress();
  const uint32_t now = millis();
  if (connected && !networkOnline) {
    networkOnline = true;
    if (!networkServicesStarted) {
      server.begin();
      discoveryUdp.begin(kDiscoveryPort);
      networkServicesStarted = true;
    }
    configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
    announceDiscovery();
    lastDiscoveryMs = now;
  } else if (!connected && networkOnline) {
    networkOnline = false;
  }
  if (!connected && now - lastWifiRetryMs >= kWifiRetryIntervalMs) {
    WiFi.reconnect();
    lastWifiRetryMs = now;
  }
}

}  // namespace

void setup() {
  ++retainedBootCount;
  bootResetReason = esp_reset_reason();
  keyboard.begin();
  consumer.begin();
  USB.productName("Room Agent TV Connector");
  USB.manufacturerName("Room Agent");
  USB.onEvent(onUsbEvent);
  usbDriverStarted = USB.begin();
  if (isInfraredProfile()) {
    // Do not call pinMode() here. On Arduino-ESP32 2.x it can prevent
    // IRremote from attaching the LEDC hardware PWM channel to the selected
    // output pin.
    IrSender.begin();
    irInitialized = true;
  }

  WiFi.mode(WIFI_STA);
  WiFi.setHostname(ROOM_AGENT_CONNECTOR_NAME);
  WiFi.begin(ROOM_AGENT_WIFI_SSID, ROOM_AGENT_WIFI_PASSWORD);
  // esp_fill_random() has its documented true-random guarantee once the Wi-Fi
  // subsystem is enabled. Network services do not start until this ID exists.
  static constexpr char kHex[] = "0123456789abcdef";
  uint8_t bootBytes[16];
  esp_fill_random(bootBytes, sizeof(bootBytes));
  for (size_t index = 0; index < sizeof(bootBytes); ++index) {
    bootId[index * 2] = kHex[bootBytes[index] >> 4];
    bootId[index * 2 + 1] = kHex[bootBytes[index] & 0x0F];
  }
  bootId[32] = '\0';
  lastWifiRetryMs = millis();
}

void loop() {
  updateNetworkState();
  if (networkOnline) {
    handleHttpClient();
    const uint32_t now = millis();
    if (now - lastDiscoveryMs >= kDiscoveryIntervalMs) {
      announceDiscovery();
      lastDiscoveryMs = now;
    }
  }
  delay(2);
}
