#pragma once

#include <stddef.h>
#include <string.h>

inline bool contractAsciiAlphaNumeric(char value) {
  return (value >= 'a' && value <= 'z') || (value >= '0' && value <= '9');
}

inline bool contractHex(char value) {
  return (value >= '0' && value <= '9') ||
         (value >= 'a' && value <= 'f');
}

inline bool validContractName(const char* value, size_t length) {
  if (value == nullptr || length == 0 || length > 64 ||
      !contractAsciiAlphaNumeric(value[0])) {
    return false;
  }
  for (size_t index = 1; index < length; ++index) {
    const char character = value[index];
    if (!contractAsciiAlphaNumeric(character) && character != '.' &&
        character != '_' && character != '-') {
      return false;
    }
  }
  return true;
}

inline bool validContractName(const char* value) {
  if (value == nullptr) return false;
  const size_t length = strnlen(value, 65);
  return length <= 64 && validContractName(value, length);
}

inline bool validContractUuid(const char* value, size_t length) {
  if (value == nullptr || length != 36) return false;
  for (size_t index = 0; index < 36; ++index) {
    const bool hyphen = index == 8 || index == 13 || index == 18 || index == 23;
    if (hyphen ? value[index] != '-' : !contractHex(value[index])) return false;
  }
  return true;
}

inline bool validContractUuid(const char* value) {
  return value != nullptr &&
         validContractUuid(value, strnlen(value, 37));
}

inline bool validContractBootId(const char* value, size_t length) {
  if (value == nullptr || length != 32) return false;
  for (size_t index = 0; index < 32; ++index) {
    if (!contractHex(value[index])) return false;
  }
  return true;
}

inline bool validContractBootId(const char* value) {
  return value != nullptr &&
         validContractBootId(value, strnlen(value, 33));
}

inline bool contractBootIdMatches(
    const char* supplied, size_t suppliedLength, const char* current) {
  return validContractBootId(supplied, suppliedLength) &&
         validContractBootId(current) &&
         memcmp(supplied, current, suppliedLength) == 0;
}

inline bool contractBootIdMatches(const char* supplied, const char* current) {
  if (supplied == nullptr) return false;
  return contractBootIdMatches(supplied, strnlen(supplied, 33), current);
}

inline bool contractBytesEqual(
    const char* supplied, size_t suppliedLength, const char* expected) {
  if (supplied == nullptr || expected == nullptr) return false;
  const size_t expectedLength = strlen(expected);
  return suppliedLength == expectedLength &&
         memcmp(supplied, expected, expectedLength) == 0;
}

inline bool validContractPrintableAscii(
    const char* value, size_t length, size_t maximum) {
  if (value == nullptr || length == 0 || length > maximum) return false;
  for (size_t index = 0; index < length; ++index) {
    const unsigned char character = static_cast<unsigned char>(value[index]);
    if (character < 0x20 || character > 0x7e) return false;
  }
  return true;
}

inline bool validContractHttpLineOctet(int value) {
  return value == 13 || (value >= 0x20 && value <= 0x7e);
}

inline bool parseContractContentLength(
    const char* value, size_t maximum, size_t& parsed) {
  parsed = 0;
  if (value == nullptr || value[0] == '\0') return false;
  for (size_t index = 0; value[index] != '\0'; ++index) {
    const char character = value[index];
    if (character < '0' || character > '9') return false;
    const size_t digit = static_cast<size_t>(character - '0');
    if (parsed > maximum / 10 ||
        (parsed == maximum / 10 && digit > maximum % 10)) {
      return false;
    }
    parsed = parsed * 10 + digit;
  }
  return parsed > 0 && parsed <= maximum;
}

inline bool validContractWait(bool isInteger, int value) {
  return isInteger && value >= 0 && value <= 5000;
}
