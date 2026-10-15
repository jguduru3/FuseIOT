// motor: hobby servo on a 50 Hz LEDC channel, with non-blocking sweeps.
//   GET /status -> {"position": float, "speed": 0-100, "moving": bool, "homed": bool}
//   POST /move {"position", "speed"?}, /speed {"speed"}, /stop, /home
//
// "position" is the commanded servo angle right now: during a sweep it moves
// at the configured speed and "moving" stays true until the target is reached.
// A hobby servo has no position feedback, so this is the pulse being sent, not
// a measurement.
#include "fuseiot_config.h"

#if FUSEIOT_CAPABILITY == FUSEIOT_CAP_MOTOR

#include "capability.h"
#include "pwm.h"
#include "util.h"

namespace {

constexpr uint32_t kServoHz = 50;
constexpr uint8_t kServoBits = 14;  // max on C3/S3; 1.2 us steps at 50 Hz
constexpr uint32_t kPeriodUs = 1000000UL / kServoHz;
constexpr uint32_t kStepMs = 5;
constexpr float kPosMin = FUSEIOT_MOTOR_POS_MIN;
constexpr float kPosMax = FUSEIOT_MOTOR_POS_MAX;
constexpr float kRangeSlack = 0.5f;  // matches FuseIoT's position tolerance

static_assert(FUSEIOT_MOTOR_POS_MAX > FUSEIOT_MOTOR_POS_MIN, "position range is empty");
static_assert(FUSEIOT_SERVO_MAX_US > FUSEIOT_SERVO_MIN_US, "servo pulse range is empty");

PwmOut g_servo;
bool g_outputOn = false;
float g_pos = kPosMin;
float g_target = kPosMin;
float g_speed = FUSEIOT_MOTOR_DEFAULT_SPEED;
bool g_moving = false;
bool g_homed = false;
bool g_homing = false;
uint32_t g_lastStep = 0;

void writePulse() {
  if (!g_outputOn) return;
  float frac = (g_pos - kPosMin) / (kPosMax - kPosMin);
  if (FUSEIOT_SERVO_INVERT) frac = 1.0f - frac;
  const float us = FUSEIOT_SERVO_MIN_US + frac * (FUSEIOT_SERVO_MAX_US - FUSEIOT_SERVO_MIN_US);
  g_servo.writeFraction(us / static_cast<float>(kPeriodUs));
}

void enableOutput() {
  if (g_outputOn) return;
  g_outputOn = true;
  writePulse();
}

void arrive() {
  g_pos = g_target;
  g_moving = false;
  if (g_homing) {
    g_homing = false;
    g_homed = true;
  }
  writePulse();
}

void moveTo(float target) {
  enableOutput();
  g_target = target;
  if (fabsf(g_target - g_pos) < 0.001f) {
    arrive();
    return;
  }
  g_moving = true;
  g_lastStep = millis();
}

int parseSpeed(JsonObjectConst body, bool required, JsonDocument& out) {
  double v = 0;
  const Field f = getNumber(body, "speed", v);
  if (f == Field::Invalid) return replyBadField(out, "speed", "must be a number 0-100");
  if (f == Field::Missing) return required ? replyBadField(out, "speed", "is required") : 0;
  g_speed = static_cast<float>(clampValue(v, 0.0, 100.0));
  return 0;
}

double round2(float v) { return round(static_cast<double>(v) * 100.0) / 100.0; }

}  // namespace

namespace capability {

void begin() {
  g_servo.begin(FUSEIOT_SERVO_PIN, 0, kServoHz, kServoBits);  // duty 0: no pulses, servo limp
#if FUSEIOT_MOTOR_HOME_ON_BOOT
  g_pos = g_target = kPosMin;
  g_homed = true;
  enableOutput();
#endif
}

void loop() {
  if (!g_moving) return;
  const uint32_t now = millis();
  const uint32_t elapsed = now - g_lastStep;
  if (elapsed < kStepMs) return;
  g_lastStep = now;
  // Speed 0 would never arrive; crawl at 1 % instead.
  const float rate = FUSEIOT_MOTOR_MAX_DEG_PER_S * (g_speed < 1.0f ? 1.0f : g_speed) / 100.0f;
  const float step = rate * static_cast<float>(elapsed) / 1000.0f;
  const float remaining = g_target - g_pos;
  if (fabsf(remaining) <= step) {
    arrive();
  } else {
    g_pos += remaining > 0 ? step : -step;
    writePulse();
  }
}

int handle(const String& path, bool isGet, JsonObjectConst body, JsonDocument& out) {
  if (path == "/status") {
    if (!isGet) return methodNotAllowed(out, "GET");
    out["position"] = round2(g_pos);
    out["speed"] = round2(g_speed);
    out["moving"] = g_moving;
    out["homed"] = g_homed;
    return 200;
  }

  const bool command = path == "/move" || path == "/speed" || path == "/stop" || path == "/home";
  if (!command) return 0;
  if (isGet) return methodNotAllowed(out, "POST");

  if (path == "/move") {
    double position = 0;
    const Field f = getNumber(body, "position", position);
    if (f != Field::Ok) return replyBadField(out, "position", "must be a number");
    if (position < kPosMin - kRangeSlack || position > kPosMax + kRangeSlack) {
      out["error"] = "position out of range";
      out["min"] = kPosMin;
      out["max"] = kPosMax;
      return 400;
    }
    if (int err = parseSpeed(body, false, out)) return err;
    g_homing = false;
    moveTo(clampValue(static_cast<float>(position), kPosMin, kPosMax));
  } else if (path == "/speed") {
    if (int err = parseSpeed(body, true, out)) return err;
  } else if (path == "/stop") {
    g_target = g_pos;
    g_moving = false;
    g_homing = false;
  } else {  // /home
    g_homing = true;
    moveTo(kPosMin);
  }
  return replyAccepted(out);
}

String describe() {
  return "servo GPIO" + String(FUSEIOT_SERVO_PIN) + ", " + String(FUSEIOT_SERVO_MIN_US) + "-" +
         String(FUSEIOT_SERVO_MAX_US) + " us for " + String(kPosMin, 1) + ".." + String(kPosMax, 1) +
         (g_homed ? ", homed" : ", not homed (no pulses until the first command)");
}

}  // namespace capability

#endif
