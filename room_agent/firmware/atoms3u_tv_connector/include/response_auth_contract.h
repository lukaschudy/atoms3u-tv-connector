#pragma once

#include <cstddef>
#include <cstdint>

constexpr char kResponseSignatureDomain[] = "room-agent-response-v1\n";
constexpr char kResponseSignatureHeader[] = "X-Room-Agent-Response-Signature";

template <typename Update>
int updateResponseAuthMessage(
    const uint8_t* body, std::size_t bodySize, Update update) {
  int result = update(
      reinterpret_cast<const uint8_t*>(kResponseSignatureDomain),
      sizeof(kResponseSignatureDomain) - 1);
  if (result != 0) return result;
  return update(body, bodySize);
}
