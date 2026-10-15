// Small helpers shared by the HTTP layer and the capabilities.
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

#include <cmath>

#include "fuseiot_config.h"

// ---- JSON request helpers -------------------------------------------------------

enum class Field : int8_t { Missing = 0, Ok = 1, Invalid = -1 };

// Reads a JSON number (integer or float). Strings and booleans are Invalid.
inline Field getNumber(JsonObjectConst body, const char* key, double& out) {
  JsonVariantConst v = body[key];
  if (v.isNull()) return Field::Missing;
  if (v.is<bool>() || !v.is<double>()) return Field::Invalid;
  out = v.as<double>();
  if (!std::isfinite(out)) return Field::Invalid;
  return Field::Ok;
}

inline Field getString(JsonObjectConst body, const char* key, const char*& out) {
  JsonVariantConst v = body[key];
  if (v.isNull()) return Field::Missing;
  if (!v.is<const char*>()) return Field::Invalid;
  out = v.as<const char*>();
  return Field::Ok;
}

inline int replyError(JsonDocument& out, int status, const char* message) {
  out.clear();
  out["error"] = message;
  return status;
}

inline int replyAccepted(JsonDocument& out) {
  out["status"] = "accepted";
  return 200;
}

// Builds "<key> must be a number" style messages without heap churn at call sites.
inline int replyBadField(JsonDocument& out, const char* key, const char* expectation) {
  out.clear();
  String msg(key);
  msg += " ";
  msg += expectation;
  out["error"] = msg;
  out["field"] = key;
  return 400;
}

template <typename T>
inline T clampValue(T v, T lo, T hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

// ---- Time --------------------------------------------------------------------

// True once NTP has set the clock (anything after 2021-01-01).
bool fuseiotTimeSynced();
// Unix time with sub-second precision (only meaningful when synced).
double fuseiotUnixTime();

// ---- Identity ------------------------------------------------------------------

const String& fuseiotDeviceId();

// ---- Constant-time comparison --------------------------------------------------

// Timing depends only on the length of `expected`, not on where `given` differs.
bool fuseiotSecureEquals(const char* given, size_t givenLen, const char* expected, size_t expectedLen);

// ---- Deferred NVS save -------------------------------------------------------------

// Coalesces bursts of state changes into one flash write.
class DeferredSave {
 public:
  void touch() {
    dirty_ = true;
    changedAt_ = millis();
  }
  bool due() const { return dirty_ && (millis() - changedAt_) >= FUSEIOT_STATE_SAVE_DELAY_MS; }
  void done() { dirty_ = false; }

 private:
  bool dirty_ = false;
  uint32_t changedAt_ = 0;
};
