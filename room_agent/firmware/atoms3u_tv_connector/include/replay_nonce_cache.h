#pragma once

#include <stddef.h>
#include <string.h>
#include <time.h>

enum class ReplayNonceResult {
  Accepted,
  Replayed,
  Full,
};

template <size_t Capacity>
class ReplayNonceCache {
 public:
  ReplayNonceCache() = default;

  ReplayNonceResult remember(const char* nonce, time_t expiresAt, time_t now) {
    clearExpired(now);
    for (const Entry& entry : entries_) {
      if (entry.occupied && strcmp(entry.nonce, nonce) == 0) {
        return ReplayNonceResult::Replayed;
      }
    }
    for (Entry& entry : entries_) {
      if (!entry.occupied) {
        memcpy(entry.nonce, nonce, 33);
        entry.expiresAt = expiresAt;
        entry.occupied = true;
        return ReplayNonceResult::Accepted;
      }
    }
    return ReplayNonceResult::Full;
  }

 private:
  struct Entry {
    char nonce[33] = {};
    time_t expiresAt = 0;
    bool occupied = false;
  };

  void clearExpired(time_t now) {
    for (Entry& entry : entries_) {
      if (entry.occupied && entry.expiresAt < now) {
        entry = Entry{};
      }
    }
  }

  Entry entries_[Capacity] = {};
};
