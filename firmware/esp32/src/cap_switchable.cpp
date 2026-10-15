// switchable: relay output, optional debounced push button, optional status LED.
//   GET /state -> {"power": bool}
//   POST /on, POST /off
#include "fuseiot_config.h"

#if FUSEIOT_CAPABILITY == FUSEIOT_CAP_SWITCHABLE

#include <Preferences.h>

#include "capability.h"
#include "util.h"

namespace {

Preferences g_prefs;
DeferredSave g_save;
bool g_power = false;

bool g_btnStable = false;  // debounced "pressed"
bool g_btnRaw = false;
uint32_t g_btnChangedAt = 0;

uint8_t level(bool active, bool activeLow) { return (active != activeLow) ? HIGH : LOW; }

void applyOutputs() {
  digitalWrite(FUSEIOT_RELAY_PIN, level(g_power, FUSEIOT_RELAY_ACTIVE_LOW));
#if FUSEIOT_STATUS_LED_PIN >= 0
  digitalWrite(FUSEIOT_STATUS_LED_PIN, level(g_power, FUSEIOT_STATUS_LED_ACTIVE_LOW));
#endif
}

void setPower(bool on, const char* source) {
  if (on == g_power) return;
  g_power = on;
  applyOutputs();
  g_save.touch();
  Serial.printf("[switch] %s (%s)\n", on ? "on" : "off", source);
}

#if FUSEIOT_BUTTON_PIN >= 0
bool buttonPressed() {
  const int v = digitalRead(FUSEIOT_BUTTON_PIN);
  return FUSEIOT_BUTTON_ACTIVE_LOW ? v == LOW : v == HIGH;
}

void pollButton() {
  const uint32_t now = millis();
  const bool raw = buttonPressed();
  if (raw != g_btnRaw) {
    g_btnRaw = raw;
    g_btnChangedAt = now;
  } else if (raw != g_btnStable && now - g_btnChangedAt >= FUSEIOT_BUTTON_DEBOUNCE_MS) {
    g_btnStable = raw;
    if (g_btnStable) setPower(!g_power, "button");  // toggle on press, not release
  }
}
#endif

}  // namespace

namespace capability {

void begin() {
  g_prefs.begin("fuseiot", false);
#if FUSEIOT_RESTORE_STATE
  g_power = g_prefs.getBool("power", false);
#endif
  // Latch the output level before enabling the driver: no relay click at boot.
  digitalWrite(FUSEIOT_RELAY_PIN, level(g_power, FUSEIOT_RELAY_ACTIVE_LOW));
  pinMode(FUSEIOT_RELAY_PIN, OUTPUT);
#if FUSEIOT_STATUS_LED_PIN >= 0
  pinMode(FUSEIOT_STATUS_LED_PIN, OUTPUT);
#endif
  applyOutputs();

#if FUSEIOT_BUTTON_PIN >= 0
  pinMode(FUSEIOT_BUTTON_PIN, FUSEIOT_BUTTON_ACTIVE_LOW ? INPUT_PULLUP : INPUT_PULLDOWN);
  g_btnRaw = g_btnStable = buttonPressed();  // a button held at boot does not toggle
  g_btnChangedAt = millis();
#endif
}

void loop() {
#if FUSEIOT_BUTTON_PIN >= 0
  pollButton();
#endif
#if FUSEIOT_RESTORE_STATE
  if (g_save.due()) {
    if (g_prefs.getBool("power", false) != g_power) g_prefs.putBool("power", g_power);
    g_save.done();
  }
#endif
}

int handle(const String& path, bool isGet, JsonObjectConst body, JsonDocument& out) {
  (void)body;
  if (path == "/state") {
    if (!isGet) return methodNotAllowed(out, "GET");
    out["power"] = g_power;
    return 200;
  }
  if (path == "/on" || path == "/off") {
    if (isGet) return methodNotAllowed(out, "POST");
    setPower(path == "/on", "api");
    return replyAccepted(out);
  }
  return 0;
}

String describe() {
  String s = "relay GPIO" + String(FUSEIOT_RELAY_PIN);
  s += FUSEIOT_RELAY_ACTIVE_LOW ? " (active low)" : " (active high)";
  s += ", button GPIO" + String(FUSEIOT_BUTTON_PIN);
  s += ", LED GPIO" + String(FUSEIOT_STATUS_LED_PIN);
  s += g_power ? ", power on" : ", power off";
  return s;
}

}  // namespace capability

#endif
