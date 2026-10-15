#include "util.h"

#include <sys/time.h>
#include <time.h>

bool fuseiotTimeSynced() { return time(nullptr) > 1609459200;  /* 2021-01-01 */ }

double fuseiotUnixTime() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return static_cast<double>(tv.tv_sec) + static_cast<double>(tv.tv_usec) / 1e6;
}

bool fuseiotSecureEquals(const char* given, size_t givenLen, const char* expected, size_t expectedLen) {
  // Loop length is fixed by the secret's length; a length mismatch is folded
  // into the result instead of returning early.
  uint8_t diff = givenLen == expectedLen ? 0 : 1;
  for (size_t i = 0; i < expectedLen; i++) {
    const uint8_t g = i < givenLen ? static_cast<uint8_t>(given[i]) : 0;
    diff |= static_cast<uint8_t>(g ^ static_cast<uint8_t>(expected[i]));
  }
  return diff == 0;
}
