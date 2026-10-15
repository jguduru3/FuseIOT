// lock: lock relay/MOSFET, optional reed switch (door_open), optional bolt
// sense switch ("jammed" detection), optional PIN code and auto-relock.
//   GET /status -> {"lock_state": "locked|unlocked|jammed", "battery_level": int|null, "door_open": bool}
//   POST /lock {"action": "lock"|"unlock", "user_code"?}
//
// Without a bolt sense switch lock_state reports the commanded relay state.
// With one, it reports what the switch sees, and "jammed" when the bolt has
// not reached the commanded position within FUSEIOT_LOCK_JAM_TIMEOUT_MS.
// The device always boots locked.
#include "fuseiot_config.h"

#if FUSEIOT_CAPABILITY == FUSEIOT_CAP_LOCK

#include "capability.h"
#include "util.h"

namespace {

constexpr uint32_t kReedDebounceMs = 50;
constexpr uint32_t kBatteryIntervalMs = 10000;

bool g_locked = true;          // commanded state
uint32_t g_commandAt = 0;      // millis() of the last lock/unlock
bool g_relockPending = false;
uint32_t g_relockAt = 0;

bool g_doorOpen = false;
bool g_reedRaw = false;
uint32_t g_reedChangedAt = 0;

uint8_t g_failedAttempts = 0;
bool g_lockedOut = false;
uint32_t g_lockoutUntil = 0;

int g_battery = -1;  // percent, -1 = unknown
#if FUSEIOT_BATTERY_ADC_PIN >= 0
uint32_t g_lastBattery = 0;
#endif

void driveRelay() {
  const bool energize = g_locked ? !FUSEIOT_LOCK_ENERGIZE_TO_UNLOCK : FUSEIOT_LOCK_ENERGIZE_TO_UNLOCK;
  digitalWrite(FUSEIOT_LOCK_RELAY_PIN, (energize != FUSEIOT_LOCK_RELAY_ACTIVE_LOW) ? HIGH : LOW);
}

void setLocked(bool locked, const char* source) {
  if (locked != g_locked) g_commandAt = millis();
  g_locked = locked;
  driveRelay();
#if FUSEIOT_LOCK_AUTO_RELOCK_S > 0
  g_relockPending = !locked;
  g_relockAt = millis() + FUSEIOT_LOCK_AUTO_RELOCK_S * 1000UL;
#endif
  Serial.printf("[lock] %s (%s)\n", locked ? "locked" : "unlocked", source);
}

#if FUSEIOT_REED_PIN >= 0
bool reedReadsOpen() {
  const int v = digitalRead(FUSEIOT_REED_PIN);
  return FUSEIOT_REED_OPEN_HIGH ? v == HIGH : v == LOW;
}
#endif

const char* lockState() {
#if FUSEIOT_LOCK_SENSE_PIN >= 0
  const int v = digitalRead(FUSEIOT_LOCK_SENSE_PIN);
  const bool boltLocked = FUSEIOT_LOCK_SENSE_LOCKED_LOW ? v == LOW : v == HIGH;
  if (boltLocked == g_locked) return g_locked ? "locked" : "unlocked";
  if (millis() - g_commandAt >= FUSEIOT_LOCK_JAM_TIMEOUT_MS) return "jammed";
  return boltLocked ? "locked" : "unlocked";  // still moving: report the truth
#else
  return g_locked ? "locked" : "unlocked";
#endif
}

#if FUSEIOT_BATTERY_ADC_PIN >= 0
void sampleBattery() {
  uint32_t sum = 0;
  for (int i = 0; i < 8; i++) sum += analogReadMilliVolts(FUSEIOT_BATTERY_ADC_PIN);
  const double mv = static_cast<double>(sum) / 8.0 * FUSEIOT_BATTERY_DIVIDER;
  const double pct = (mv - FUSEIOT_BATTERY_EMPTY_MV) * 100.0 /
                     static_cast<double>(FUSEIOT_BATTERY_FULL_MV - FUSEIOT_BATTERY_EMPTY_MV);
  const int value = static_cast<int>(lround(clampValue(pct, 0.0, 100.0)));
  g_battery = g_battery < 0 ? value : (g_battery * 3 + value) / 4;  // light smoothing
}
#endif

// Accepts "1234" or 1234.
bool codeMatches(JsonObjectConst body) {
  static const size_t expectedLen = strlen(FUSEIOT_LOCK_PIN_CODE);
  JsonVariantConst v = body["user_code"];
  String given;
  if (v.is<const char*>()) {
    given = v.as<const char*>();
  } else if (v.is<long>() && !v.is<bool>()) {
    given = String(v.as<long>());
  }
  return fuseiotSecureEquals(given.c_str(), given.length(), FUSEIOT_LOCK_PIN_CODE, expectedLen);
}

}  // namespace

namespace capability {

void begin() {
  g_locked = true;
  driveRelay();  // latch the level before enabling the output driver
  pinMode(FUSEIOT_LOCK_RELAY_PIN, OUTPUT);
  driveRelay();
#if FUSEIOT_REED_PIN >= 0
  pinMode(FUSEIOT_REED_PIN, INPUT_PULLUP);
  g_reedRaw = g_doorOpen = reedReadsOpen();
#endif
#if FUSEIOT_LOCK_SENSE_PIN >= 0
  pinMode(FUSEIOT_LOCK_SENSE_PIN, INPUT_PULLUP);
#endif
#if FUSEIOT_BATTERY_ADC_PIN >= 0
  analogReadResolution(12);
  sampleBattery();
  g_lastBattery = millis();
#endif
  g_commandAt = millis();
}

void loop() {
  const uint32_t now = millis();
#if FUSEIOT_REED_PIN >= 0
  const bool raw = reedReadsOpen();
  if (raw != g_reedRaw) {
    g_reedRaw = raw;
    g_reedChangedAt = now;
  } else if (raw != g_doorOpen && now - g_reedChangedAt >= kReedDebounceMs) {
    g_doorOpen = raw;
    Serial.printf("[lock] door %s\n", g_doorOpen ? "opened" : "closed");
  }
#endif
  // Auto-relock once the delay has passed and the door is closed.
  if (g_relockPending && static_cast<int32_t>(now - g_relockAt) >= 0 && !g_doorOpen) {
    setLocked(true, "auto-relock");
  }
  if (g_lockedOut && static_cast<int32_t>(now - g_lockoutUntil) >= 0) {
    g_lockedOut = false;
    g_failedAttempts = 0;
  }
#if FUSEIOT_BATTERY_ADC_PIN >= 0
  if (now - g_lastBattery >= kBatteryIntervalMs) {
    g_lastBattery = now;
    sampleBattery();
  }
#endif
}

int handle(const String& path, bool isGet, JsonObjectConst body, JsonDocument& out) {
  if (path == "/status") {
    if (!isGet) return methodNotAllowed(out, "GET");
    out["lock_state"] = lockState();
    if (g_battery >= 0) {
      out["battery_level"] = g_battery;
    } else {
      out["battery_level"] = nullptr;
    }
    out["door_open"] = g_doorOpen;
    return 200;
  }
  if (path != "/lock") return 0;
  if (isGet) return methodNotAllowed(out, "POST");

  const char* action = "lock";
  const Field f = getString(body, "action", action);
  if (f == Field::Invalid) return replyBadField(out, "action", "must be \"lock\" or \"unlock\"");
  const bool unlock = strcmp(action, "unlock") == 0;
  if (!unlock && strcmp(action, "lock") != 0) {
    return replyBadField(out, "action", "must be \"lock\" or \"unlock\"");
  }

  if (unlock) {
    if (strlen(FUSEIOT_LOCK_PIN_CODE) > 0) {
      if (g_lockedOut) {
        out["error"] = "too many failed attempts";
        out["retry_after_s"] = (g_lockoutUntil - millis()) / 1000 + 1;
        return 429;
      }
      if (!codeMatches(body)) {
        if (++g_failedAttempts >= FUSEIOT_LOCK_MAX_ATTEMPTS) {
          g_lockedOut = true;
          g_lockoutUntil = millis() + FUSEIOT_LOCK_LOCKOUT_S * 1000UL;
          Serial.println(F("[lock] too many wrong codes, locked out"));
        }
        return replyError(out, 403, "invalid user_code");
      }
      g_failedAttempts = 0;
    }
    setLocked(false, "api");
  } else {
#if FUSEIOT_LOCK_REFUSE_WHEN_OPEN && FUSEIOT_REED_PIN >= 0
    if (g_doorOpen) return replyError(out, 409, "door is open");
#endif
    setLocked(true, "api");
  }
  return replyAccepted(out);
}

String describe() {
  String s = "lock relay GPIO" + String(FUSEIOT_LOCK_RELAY_PIN) + ", reed GPIO" + String(FUSEIOT_REED_PIN) +
             ", sense GPIO" + String(FUSEIOT_LOCK_SENSE_PIN);
  s += strlen(FUSEIOT_LOCK_PIN_CODE) > 0 ? ", PIN required" : ", no PIN";
  if (FUSEIOT_LOCK_AUTO_RELOCK_S > 0) s += ", auto-relock " + String(FUSEIOT_LOCK_AUTO_RELOCK_S) + " s";
  return s;
}

}  // namespace capability

#endif
