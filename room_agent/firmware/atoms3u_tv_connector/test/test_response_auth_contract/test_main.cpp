#include <unity.h>

#include <cstdint>
#include <string>

#include "response_auth_contract.h"

void setUp() {}
void tearDown() {}

namespace {

void test_response_auth_message_matches_python_vector() {
  constexpr char body[] = "{\"status\":\"executed\"}";
  std::string message;

  const int result = updateResponseAuthMessage(
      reinterpret_cast<const uint8_t*>(body), sizeof(body) - 1,
      [&message](const uint8_t* chunk, size_t size) {
        message.append(reinterpret_cast<const char*>(chunk), size);
        return 0;
      });

  TEST_ASSERT_EQUAL_INT(0, result);
  TEST_ASSERT_EQUAL_STRING(
      "room-agent-response-v1\n{\"status\":\"executed\"}", message.c_str());
  TEST_ASSERT_EQUAL_STRING(
      "X-Room-Agent-Response-Signature", kResponseSignatureHeader);
}

}  // namespace

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_response_auth_message_matches_python_vector);
  return UNITY_END();
}
