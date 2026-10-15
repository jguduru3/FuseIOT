// dimmable: one LEDC PWM output with non-blocking fades.
//   GET /state -> {"power": bool, "brightness": 0-100}
//   POST /on {"brightness"?}, /off, /brightness {"brightness"}, /fade {"brightness","duration_ms"}
//
// "brightness" always reports the level the output is at right now; during a
// fade it moves towards the target and equals it once the fade completes, so
// FuseIoT's confirmation (brightness == target) is truthful.
#include "fuseiot_config.h"

#if FUSEIOT_CAPABILITY == FUSEIOT_CAP_DIMMABLE

#include <Preferences.h>

#include "capability.h"
#include "pwm.h"
#include "util.h"

namespace {

constexpr uint32_t kMaxFadeMs = 600000;  // 10 minutes
constexpr uint32_t kDefaultFadeMs = 1000;

Preferences g_prefs;
DeferredSave g_save;
PwmOut g_pwm;

bool g_power = false;
int g_target = 100;     // commanded brightness 0..100
float g_level = 100.0f;  // current brightness (moves during a fade)
bool g_fading = false;
float g_fadeFrom = 0.0f;
uint32_t g_fadeStart = 0;
uint32_t g_fadeMs = 0;

void output() {
  const float level = g_power ? fuseiotGamma(g_level / 100.0f, FUSEIOT_PWM_GAMMA) : 0.0f;
  g_pwm.writeFraction(level);
}

int reportedBrightness() { return static_cast<int>(lroundf(g_level)); }

void setBrightnessNow(int value) {
  g_fading = false;
  g_target = value;
  g_level = static_cast<float>(value);
  output();
  g_save.touch();
}

void startFade(int value, uint32_t durationMs) {
  if (durationMs == 0) {
    setBrightnessNow(value);
    return;
  }
  g_target = value;
  g_fadeFrom = g_level;
  g_fadeStart = millis();
  g_fadeMs = durationMs;
  g_fading = true;
  g_save.touch();
}

void setPower(bool on) {
  if (on != g_power) g_save.touch();
  g_power = on;
  output();
}

// Parses a required/optional "brightness" field into 0..100.
// Returns 0 on success, otherwise the HTTP status already written to `out`.
int parseBrightness(JsonObjectConst body, bool required, bool& present, int& value, JsonDocument& out) {
  double v = 0;
  const Field f = getNumber(body, "brightness", v);
  present = f == Field::Ok;
  if (f == Field::Invalid) return replyBadField(out, "brightness", "must be a number 0-100");
  if (f == Field::Missing && required) return replyBadField(out, "brightness", "is required");
  if (present) value = static_cast<int>(lround(clampValue(v, 0.0, 100.0)));
  return 0;
}

}  // namespace

namespace capability {

void begin() {
  g_prefs.begin("fuseiot", false);
#if FUSEIOT_RESTORE_STATE
  g_power = g_prefs.getBool("power", false);
  g_target = clampValue<int>(g_prefs.getUChar("bri", 100), 0, 100);
  g_level = static_cast<float>(g_target);
#endif
  g_pwm.begin(FUSEIOT_PWM_PIN, 0, FUSEIOT_PWM_FREQ_HZ, FUSEIOT_PWM_RESOLUTION_BITS, FUSEIOT_PWM_INVERT);
  output();
}

void loop() {
  if (g_fading) {
    const uint32_t elapsed = millis() - g_fadeStart;
    if (elapsed >= g_fadeMs) {
      g_fading = false;
      g_level = static_cast<float>(g_target);
    } else {
      const float t = static_cast<float>(elapsed) / static_cast<float>(g_fadeMs);
      g_level = g_fadeFrom + (static_cast<float>(g_target) - g_fadeFrom) * t;
    }
    output();
  }
#if FUSEIOT_RESTORE_STATE
  if (g_save.due()) {
    if (g_prefs.getBool("power", false) != g_power) g_prefs.putBool("power", g_power);
    if (g_prefs.getUChar("bri", 255) != g_target) g_prefs.putUChar("bri", static_cast<uint8_t>(g_target));
    g_save.done();
  }
#endif
}

int handle(const String& path, bool isGet, JsonObjectConst body, JsonDocument& out) {
  if (path == "/state") {
    if (!isGet) return methodNotAllowed(out, "GET");
    out["power"] = g_power;
    out["brightness"] = reportedBrightness();
    return 200;
  }

  const bool command = path == "/on" || path == "/off" || path == "/brightness" || path == "/fade";
  if (!command) return 0;
  if (isGet) return methodNotAllowed(out, "POST");

  bool present = false;
  int value = 0;
  if (path == "/on") {
    if (int err = parseBrightness(body, false, present, value, out)) return err;
    if (present) {
      setBrightnessNow(value);
    } else if (g_target == 0 && !g_fading) {
      setBrightnessNow(100);  // a bare "on" must give light
    }
    setPower(true);
  } else if (path == "/off") {
    setPower(false);
  } else if (path == "/brightness") {
    if (int err = parseBrightness(body, true, present, value, out)) return err;
    setBrightnessNow(value);
  } else {  // /fade
    if (int err = parseBrightness(body, true, present, value, out)) return err;
    double duration = kDefaultFadeMs;
    const Field f = getNumber(body, "duration_ms", duration);
    if (f == Field::Invalid || duration < 0) {
      return replyBadField(out, "duration_ms", "must be a number >= 0");
    }
    startFade(value, static_cast<uint32_t>(clampValue(duration, 0.0, static_cast<double>(kMaxFadeMs))));
  }
  return replyAccepted(out);
}

String describe() {
  return "PWM GPIO" + String(FUSEIOT_PWM_PIN) + " @ " + String(FUSEIOT_PWM_FREQ_HZ) + " Hz, " +
         String(FUSEIOT_PWM_RESOLUTION_BITS) + " bit, power " + (g_power ? "on" : "off") +
         ", brightness " + String(g_target);
}

}  // namespace capability

#endif
