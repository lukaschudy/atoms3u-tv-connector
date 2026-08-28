#include <unity.h>

#include "replay_nonce_cache.h"

void setUp() {}
void tearDown() {}

namespace {

constexpr time_t kExpiry = 1030;

void test_rejects_duplicate_nonce_until_expiry() {
  ReplayNonceCache<4> cache;

  TEST_ASSERT_EQUAL(
      static_cast<int>(ReplayNonceResult::Accepted),
      static_cast<int>(cache.remember("00000000000000000000000000000001", kExpiry, 1000)));
  TEST_ASSERT_EQUAL(
      static_cast<int>(ReplayNonceResult::Replayed),
      static_cast<int>(cache.remember("00000000000000000000000000000001", kExpiry, 1001)));
}

void test_full_cache_fails_closed_without_evicting_live_nonce() {
  ReplayNonceCache<2> cache;

  TEST_ASSERT_EQUAL(
      static_cast<int>(ReplayNonceResult::Accepted),
      static_cast<int>(cache.remember("00000000000000000000000000000001", kExpiry, 1000)));
  TEST_ASSERT_EQUAL(
      static_cast<int>(ReplayNonceResult::Accepted),
      static_cast<int>(cache.remember("00000000000000000000000000000002", kExpiry, 1000)));
  TEST_ASSERT_EQUAL(
      static_cast<int>(ReplayNonceResult::Full),
      static_cast<int>(cache.remember("00000000000000000000000000000003", kExpiry, 1000)));
  TEST_ASSERT_EQUAL(
      static_cast<int>(ReplayNonceResult::Replayed),
      static_cast<int>(cache.remember("00000000000000000000000000000001", kExpiry, 1000)));
}

void test_expired_slot_can_be_reused() {
  ReplayNonceCache<1> cache;

  TEST_ASSERT_EQUAL(
      static_cast<int>(ReplayNonceResult::Accepted),
      static_cast<int>(cache.remember("00000000000000000000000000000001", 1000, 1000)));
  TEST_ASSERT_EQUAL(
      static_cast<int>(ReplayNonceResult::Accepted),
      static_cast<int>(cache.remember("00000000000000000000000000000002", 1031, 1001)));
}

}  // namespace

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_rejects_duplicate_nonce_until_expiry);
  RUN_TEST(test_full_cache_fails_closed_without_evicting_live_nonce);
  RUN_TEST(test_expired_slot_can_be_reused);
  return UNITY_END();
}
