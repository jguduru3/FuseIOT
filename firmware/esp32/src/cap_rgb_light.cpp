// rgb_light: three LEDC PWM channels with non-blocking effects.
//   GET /state -> {"power","brightness","r","g","b","color_mode","effect","speed"}
//   POST /on {"r","g","b","brightness"?}, /off, /color {"r","g","b","brightness"?},
//        /brightness {"brightness"}, /effect {"effect","speed"?}
//
// r/g/b report the configured base colour. While an effect runs, the output
// animates (rainbow: hue cycle; pulse: base colour breathing) and the state
// says so via "effect" and "color_mode": "effect". Setting a colour stops any
// effect, so the reported colour is always what the LEDs show when effect is
// "none".
#include "fuseiot_config.h"

#if FUSEIOT_CAPABILITY == FUSEIOT_CAP_RGB_LIGHT

#include <Preferences.h>

#include "capability.h"
#include "pwm.h"
#include "util.h"

namespace {

enum class Effect : uint8_t { None = 0, Rainbow = 1, Pulse = 2 };
const char* const kEffectNames[] = {"none", "rainbow", "pulse"};
constexpr uint8_t kEffectCount = sizeof(kEffectNames) / sizeof(kEffectNames[0]);
constexpr uint32_t kFrameMs = 10;

struct Persisted {
  uint8_t version;
  uint8_t power;
  uint8_t brightness;
  uint8_t r, g, b;
  uint8_t effect;
  uint8_t speed;
};
constexpr uint8_t kPersistVersion = 1;

Preferences g_prefs;
DeferredSave g_save;
PwmOut g_out[3];

bool g_power = false;
int g_brightness = 100;
uint8_t g_rgb[3] = {255, 255, 255};
Effect g_effect = Effect::None;
int g_speed = 50;
uint32_t g_effectStart = 0;
uint32_t g_lastFrame = 0;

// Effect period: speed 0 -> slow (30 s rainbow / 8 s pulse), 100 -> fast (0.5 s / 0.4 s).
float periodMs(float slowMs, float fastMs) {
  const float s = static_cast<float>(g_speed) / 100.0f;
  return slowMs * powf(fastMs / slowMs, s);
}

void hsvToRgb(float h, float out[3]) {  // h in [0,1), full saturation and value
  const float h6 = h * 6.0f;
  const int sector = static_cast<int>(h6) % 6;
  const float f = h6 - floorf(h6);
  const float q = 1.0f - f;
  switch (sector) {
    case 0: out[0] = 1; out[1] = f; out[2] = 0; break;
    case 1: out[0] = q; out[1] = 1; out[2] = 0; break;
    case 2: out[0] = 0; out[1] = 1; out[2] = f; break;
    case 3: out[0] = 0; out[1] = q; out[2] = 1; break;
    case 4: out[0] = f; out[1] = 0; out[2] = 1; break;
    default: out[0] = 1; out[1] = 0; out[2] = q; break;
  }
}

void render() {
  float rgb[3] = {g_rgb[0] / 255.0f, g_rgb[1] / 255.0f, g_rgb[2] / 255.0f};
  float scale = static_cast<float>(g_brightness) / 100.0f;
  const float t = static_cast<float>(millis() - g_effectStart);

  if (g_effect == Effect::Rainbow) {
    const float period = periodMs(30000.0f, 500.0f);
    hsvToRgb(fmodf(t, period) / period, rgb);
  } else if (g_effect == Effect::Pulse) {
    const float period = periodMs(8000.0f, 400.0f);
    const float phase = fmodf(t, period) / period;
    scale *= 0.05f + 0.95f * (0.5f - 0.5f * cosf(2.0f * static_cast<float>(PI) * phase));
  }

  for (int i = 0; i < 3; i++) {
    const float level = g_power ? fuseiotGamma(rgb[i] * scale, FUSEIOT_PWM_GAMMA) : 0.0f;
    g_out[i].writeFraction(level);
  }
}

void changed() {
  render();
  g_save.touch();
}

const char* effectName() { return kEffectNames[static_cast<uint8_t>(g_effect)]; }

void saveIfDue() {
#if FUSEIOT_RESTORE_STATE
  if (!g_save.due()) return;
  Persisted p = {kPersistVersion,          static_cast<uint8_t>(g_power),
                 static_cast<uint8_t>(g_brightness), g_rgb[0], g_rgb[1], g_rgb[2],
                 static_cast<uint8_t>(g_effect),    static_cast<uint8_t>(g_speed)};
  Persisted old = {};
  if (g_prefs.getBytes("rgb", &old, sizeof(old)) != sizeof(old) || memcmp(&old, &p, sizeof(p)) != 0) {
    g_prefs.putBytes("rgb", &p, sizeof(p));
  }
  g_save.done();
#endif
}

// Optional 0..max integer field. Returns 0 on success or an HTTP error status.
int parseInt(JsonObjectConst body, const char* key, int maxValue, bool required, bool& present,
             int& value, JsonDocument& out) {
  double v = 0;
  const Field f = getNumber(body, key, v);
  present = f == Field::Ok;
  if (f == Field::Invalid) {
    return replyBadField(out, key, maxValue == 255 ? "must be a number 0-255" : "must be a number 0-100");
  }
  if (f == Field::Missing && required) return replyBadField(out, key, "is required");
  if (present) value = static_cast<int>(lround(clampValue(v, 0.0, static_cast<double>(maxValue))));
  return 0;
}

// Reads r/g/b (+ optional brightness) and applies them. requireColor: /color.
int applyColorFields(JsonObjectConst body, bool requireColor, bool& anyColor, JsonDocument& out) {
  static const char* const keys[3] = {"r", "g", "b"};
  int values[3] = {g_rgb[0], g_rgb[1], g_rgb[2]};
  anyColor = false;
  for (int i = 0; i < 3; i++) {
    bool present = false;
    if (int err = parseInt(body, keys[i], 255, requireColor, present, values[i], out)) return err;
    anyColor = anyColor || present;
  }
  bool briPresent = false;
  int bri = g_brightness;
  if (int err = parseInt(body, "brightness", 100, false, briPresent, bri, out)) return err;

  if (anyColor) {
    for (int i = 0; i < 3; i++) g_rgb[i] = static_cast<uint8_t>(values[i]);
    g_effect = Effect::None;  // an explicit colour replaces any running effect
  }
  if (briPresent) g_brightness = bri;
  return 0;
}

}  // namespace

namespace capability {

void begin() {
  g_prefs.begin("fuseiot", false);
#if FUSEIOT_RESTORE_STATE
  Persisted p = {};
  if (g_prefs.getBytes("rgb", &p, sizeof(p)) == sizeof(p) && p.version == kPersistVersion) {
    g_power = p.power != 0;
    g_brightness = clampValue<int>(p.brightness, 0, 100);
    g_rgb[0] = p.r;
    g_rgb[1] = p.g;
    g_rgb[2] = p.b;
    g_effect = p.effect < kEffectCount ? static_cast<Effect>(p.effect) : Effect::None;
    g_speed = clampValue<int>(p.speed, 0, 100);
  }
#endif
  const bool invert = FUSEIOT_RGB_COMMON_ANODE != FUSEIOT_PWM_INVERT;
  const int pins[3] = {FUSEIOT_RGB_PIN_R, FUSEIOT_RGB_PIN_G, FUSEIOT_RGB_PIN_B};
  for (int i = 0; i < 3; i++) {
    g_out[i].begin(pins[i], static_cast<uint8_t>(i), FUSEIOT_PWM_FREQ_HZ, FUSEIOT_PWM_RESOLUTION_BITS,
                   invert);
  }
  g_effectStart = millis();
  render();
}

void loop() {
  if (g_power && g_effect != Effect::None && millis() - g_lastFrame >= kFrameMs) {
    g_lastFrame = millis();
    render();
  }
  saveIfDue();
}

int handle(const String& path, bool isGet, JsonObjectConst body, JsonDocument& out) {
  if (path == "/state") {
    if (!isGet) return methodNotAllowed(out, "GET");
    out["power"] = g_power;
    out["brightness"] = g_brightness;
    out["r"] = g_rgb[0];
    out["g"] = g_rgb[1];
    out["b"] = g_rgb[2];
    out["color_mode"] = g_effect == Effect::None ? "rgb" : "effect";
    out["effect"] = effectName();
    out["speed"] = g_speed;
    return 200;
  }

  const bool command = path == "/on" || path == "/off" || path == "/color" || path == "/brightness" ||
                       path == "/effect";
  if (!command) return 0;
  if (isGet) return methodNotAllowed(out, "POST");

  if (path == "/on") {
    bool anyColor = false;
    const int before = g_brightness;
    if (int err = applyColorFields(body, false, anyColor, out)) return err;
    // A bare "on" at brightness 0 would stay dark; use full brightness then.
    if (g_brightness == 0 && before == 0 && body["brightness"].isNull()) g_brightness = 100;
    g_power = true;
  } else if (path == "/off") {
    g_power = false;
  } else if (path == "/color") {
    bool anyColor = false;
    if (int err = applyColorFields(body, true, anyColor, out)) return err;
  } else if (path == "/brightness") {
    bool present = false;
    int value = g_brightness;
    if (int err = parseInt(body, "brightness", 100, true, present, value, out)) return err;
    g_brightness = value;
  } else {  // /effect
    const char* name = nullptr;
    const Field f = getString(body, "effect", name);
    if (f != Field::Ok) return replyBadField(out, "effect", "is required (none, rainbow, pulse)");
    int index = -1;
    for (uint8_t i = 0; i < kEffectCount; i++) {
      if (strcmp(name, kEffectNames[i]) == 0) index = i;
    }
    if (index < 0) {
      out.clear();
      out["error"] = "unsupported effect";
      JsonArray supported = out["supported"].to<JsonArray>();
      for (uint8_t i = 0; i < kEffectCount; i++) supported.add(kEffectNames[i]);
      return 400;
    }
    bool present = false;
    int speed = g_speed;
    if (int err = parseInt(body, "speed", 100, false, present, speed, out)) return err;
    g_speed = speed;
    g_effect = static_cast<Effect>(index);
    g_effectStart = millis();
  }
  changed();
  return replyAccepted(out);
}

String describe() {
  return "PWM R/G/B GPIO" + String(FUSEIOT_RGB_PIN_R) + "/" + String(FUSEIOT_RGB_PIN_G) + "/" +
         String(FUSEIOT_RGB_PIN_B) + (FUSEIOT_RGB_COMMON_ANODE ? " (common anode)" : "") + ", power " +
         (g_power ? "on" : "off") + ", effect " + effectName();
}

}  // namespace capability

#endif
