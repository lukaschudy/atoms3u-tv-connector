#include <unity.h>

#include "firmware_contract.h"

void setUp() {}
void tearDown() {}

namespace {

void test_accepts_canonical_uuid_and_rejects_malformed_values() {
  TEST_ASSERT_TRUE(validContractUuid("8dc4207a-65b9-4606-b5af-a1f5613f8957"));
  TEST_ASSERT_FALSE(validContractUuid("8DC4207A-65B9-4606-B5AF-A1F5613F8957"));
  TEST_ASSERT_FALSE(validContractUuid("8dc4207a65b94606b5afa1f5613f8957"));
  TEST_ASSERT_FALSE(validContractUuid("a"));
  TEST_ASSERT_FALSE(validContractUuid(""));
  TEST_ASSERT_FALSE(validContractUuid("not-a-request-id"));
  TEST_ASSERT_FALSE(validContractUuid(nullptr));
}

void test_boot_ids_require_exact_lowercase_hex() {
  const char* current = "0123456789abcdef0123456789abcdef";
  TEST_ASSERT_TRUE(validContractBootId(current));
  TEST_ASSERT_FALSE(validContractBootId("0123456789ABCDEF0123456789ABCDEF"));
  TEST_ASSERT_FALSE(validContractBootId("0123456789abcdef0123456789abcde"));
  TEST_ASSERT_FALSE(validContractBootId("a"));
  TEST_ASSERT_FALSE(validContractBootId(""));
  TEST_ASSERT_FALSE(validContractBootId("0123456789abcdef0123456789abcdef0"));
  TEST_ASSERT_FALSE(validContractBootId(nullptr));
  TEST_ASSERT_TRUE(contractBootIdMatches(current, current));
  TEST_ASSERT_FALSE(contractBootIdMatches(
      "fedcba9876543210fedcba9876543210", current));
}

void test_names_match_the_host_contract() {
  TEST_ASSERT_TRUE(validContractName("media.pause"));
  TEST_ASSERT_TRUE(validContractName("lg-tv-ir-v1"));
  TEST_ASSERT_FALSE(validContractName("Uppercase"));
  TEST_ASSERT_FALSE(validContractName(""));
  TEST_ASSERT_FALSE(validContractName(nullptr));
}

void test_explicit_lengths_reject_embedded_nuls_and_controls() {
  const char uuidWithNul[] =
      "8dc4207a-65b9-4606-b5af-a1f5613f8957\0x";
  const char bootWithNul[] =
      "0123456789abcdef0123456789abcdef\0x";
  const char nameWithNul[] = "media\0.pause";
  const char literalWithNul[] = "key\0junk";
  const char textWithNul[] = "hello\0world";
  TEST_ASSERT_FALSE(validContractUuid(uuidWithNul, sizeof(uuidWithNul) - 1));
  TEST_ASSERT_FALSE(validContractBootId(bootWithNul, sizeof(bootWithNul) - 1));
  TEST_ASSERT_FALSE(validContractName(nameWithNul, sizeof(nameWithNul) - 1));
  TEST_ASSERT_FALSE(contractBytesEqual(
      literalWithNul, sizeof(literalWithNul) - 1, "key"));
  TEST_ASSERT_FALSE(validContractPrintableAscii(
      textWithNul, sizeof(textWithNul) - 1, 120));
  TEST_ASSERT_TRUE(validContractUuid(
      "8dc4207a-65b9-4606-b5af-a1f5613f8957", 36));
  TEST_ASSERT_TRUE(validContractBootId(
      "0123456789abcdef0123456789abcdef", 32));
  TEST_ASSERT_TRUE(contractBytesEqual("text", 4, "text"));
  TEST_ASSERT_TRUE(validContractPrintableAscii("hello", 5, 120));

  TEST_ASSERT_TRUE(validContractHttpLineOctet(' '));
  TEST_ASSERT_TRUE(validContractHttpLineOctet('~'));
  TEST_ASSERT_TRUE(validContractHttpLineOctet(13));
  TEST_ASSERT_FALSE(validContractHttpLineOctet(0));
  TEST_ASSERT_FALSE(validContractHttpLineOctet('\t'));
  TEST_ASSERT_FALSE(validContractHttpLineOctet(127));
  TEST_ASSERT_FALSE(validContractHttpLineOctet(255));
}

void test_waits_require_integer_type_and_bounds() {
  TEST_ASSERT_TRUE(validContractWait(true, 0));
  TEST_ASSERT_TRUE(validContractWait(true, 5000));
  TEST_ASSERT_FALSE(validContractWait(false, 100));
  TEST_ASSERT_FALSE(validContractWait(true, -1));
  TEST_ASSERT_FALSE(validContractWait(true, 5001));
}

void test_content_length_is_strict_and_bounded_before_body_read() {
  size_t parsed = 999;
  TEST_ASSERT_TRUE(parseContractContentLength("1", 8192, parsed));
  TEST_ASSERT_EQUAL_UINT32(1, parsed);
  TEST_ASSERT_TRUE(parseContractContentLength("8192", 8192, parsed));
  TEST_ASSERT_EQUAL_UINT32(8192, parsed);
  TEST_ASSERT_FALSE(parseContractContentLength("0", 8192, parsed));
  TEST_ASSERT_FALSE(parseContractContentLength("8193", 8192, parsed));
  TEST_ASSERT_FALSE(parseContractContentLength("-1", 8192, parsed));
  TEST_ASSERT_FALSE(parseContractContentLength("12x", 8192, parsed));
  TEST_ASSERT_FALSE(
      parseContractContentLength("18446744073709551615", 8192, parsed));
  TEST_ASSERT_FALSE(parseContractContentLength("", 8192, parsed));
  TEST_ASSERT_FALSE(parseContractContentLength(nullptr, 8192, parsed));
}

}  // namespace

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_accepts_canonical_uuid_and_rejects_malformed_values);
  RUN_TEST(test_boot_ids_require_exact_lowercase_hex);
  RUN_TEST(test_names_match_the_host_contract);
  RUN_TEST(test_explicit_lengths_reject_embedded_nuls_and_controls);
  RUN_TEST(test_waits_require_integer_type_and_bounds);
  RUN_TEST(test_content_length_is_strict_and_bounded_before_body_read);
  return UNITY_END();
}
