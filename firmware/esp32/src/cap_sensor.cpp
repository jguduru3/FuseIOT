// sensor: analog probe (linear mV scaling) or DHT22/AM2302 (bit-banged here,
// no library).
//   GET /reading -> {"value": float, "unit": str, "timestamp"?: unix s, "calibrated": bool}
//   POST /calibrate {"reference": float}   (or {"reset": true} to clear)
//
// Sampling happens in loop(); /reading returns the latest good sample with the
// time it was taken (omitted until NTP has synced). If no good sample is newer
// than FUSEIOT_SENSOR_STALE_MS the device answers 503 with a JSON error.
#include "fuseiot_config.h"

#if FUSEIOT_CAPABILITY == FUSEIOT_CAP_SENSOR

#include <Preferences.h>

#include "capability.h"
#include "util.h"

namespace {

Preferences g_prefs;

bool g_haveGood = false;
double g_raw = 0.0;         // last good uncalibrated value
uint32_t g_goodAtMs = 0;    // millis() of that sample
double g_goodAtUnix = 0.0;  // unix time of that sample (0 = clock not synced)
uint32_t g_lastPoll = 0;
bool g_polledOnce = false;
uint32_t g_failures = 0;
const char* g_lastError = "no sample yet";

bool g_calibrated = false;
double g_offset = 0.0;

bool inValidRange(double v) { return v >= FUSEIOT_SENSOR_VALID_MIN && v <= FUSEIOT_SENSOR_VALID_MAX; }

#if FUSEIOT_SENSOR_SOURCE == FUSEIOT_SENSOR_ADC
// ---------------------------------------------------------------- ADC source

bool readSource(double& value, const char*& error) {
  uint32_t sum = 0;
  for (int i = 0; i < FUSEIOT_ADC_SAMPLES; i++) sum += analogReadMilliVolts(FUSEIOT_ADC_PIN);
  const double mv = static_cast<double>(sum) / FUSEIOT_ADC_SAMPLES;
  value = mv * FUSEIOT_ADC_SCALE + FUSEIOT_ADC_OFFSET;
  error = nullptr;
  return true;
}

void beginSource() {
  analogReadResolution(12);
  pinMode(FUSEIOT_ADC_PIN, INPUT);
}

String sourceName() { return "ADC GPIO" + String(FUSEIOT_ADC_PIN); }

#else
// -------------------------------------------------------------- DHT22 source
//
// Protocol: host pulls the line low >= 1 ms and releases it; the sensor answers
// 80 us low + 80 us high, then sends 40 bits. Each bit is ~50 us low followed by
// ~27 us high (0) or ~70 us high (1). Data: humidity (16 bit, x10), temperature
// (15 bit, x10, MSB = sign), checksum.

portMUX_TYPE g_dhtMux = portMUX_INITIALIZER_UNLOCKED;

// Microseconds the line stayed at `level`, or -1 on timeout.
int32_t pulseWhile(int level, uint32_t timeoutUs) {
  const uint32_t start = micros();
  while (digitalRead(FUSEIOT_DHT_PIN) == level) {
    if (micros() - start > timeoutUs) return -1;
  }
  return static_cast<int32_t>(micros() - start);
}

bool readDht(float& temperature, float& humidity, const char*& error) {
  uint8_t data[5] = {0, 0, 0, 0, 0};
  int32_t lows[40];
  int32_t highs[40];

  // Start signal (outside the critical section; 1.2 ms is fine to be interrupted).
  pinMode(FUSEIOT_DHT_PIN, OUTPUT);
  digitalWrite(FUSEIOT_DHT_PIN, LOW);
  delayMicroseconds(1200);

  bool ok = true;
  error = nullptr;
  // Release the line (pinMode may log, so not inside the critical section). The
  // sensor answers 20-40 us later; pulseWhile() copes if it already has.
  pinMode(FUSEIOT_DHT_PIN, INPUT_PULLUP);
  portENTER_CRITICAL(&g_dhtMux);  // ~5 ms of timing-critical reading
  if (pulseWhile(HIGH, 200) < 0) {  // wait for the sensor to pull low
    ok = false;
    error = "no response from DHT22 (check wiring and pull-up)";
  } else if (pulseWhile(LOW, 120) < 0 || pulseWhile(HIGH, 120) < 0) {
    ok = false;
    error = "DHT22 handshake timeout";
  } else {
    for (int i = 0; i < 40; i++) {
      lows[i] = pulseWhile(LOW, 100);
      highs[i] = pulseWhile(HIGH, 120);
      if (lows[i] < 0 || highs[i] < 0) {
        ok = false;
        error = "DHT22 bit timeout";
        break;
      }
    }
  }
  portEXIT_CRITICAL(&g_dhtMux);
  if (!ok) return false;

  for (int i = 0; i < 40; i++) {
    // A '1' has a longer high phase than the preceding ~50 us low phase.
    data[i / 8] = static_cast<uint8_t>(data[i / 8] << 1) | (highs[i] > lows[i] ? 1 : 0);
  }
  const uint8_t sum = static_cast<uint8_t>(data[0] + data[1] + data[2] + data[3]);
  if (sum != data[4]) {
    error = "DHT22 checksum mismatch";
    return false;
  }
  if ((data[0] | data[1] | data[2] | data[3]) == 0) {
    error = "DHT22 returned all zeros";
    return false;
  }
  humidity = static_cast<float>((data[0] << 8) | data[1]) * 0.1f;
  temperature = static_cast<float>(((data[2] & 0x7F) << 8) | data[3]) * 0.1f;
  if (data[2] & 0x80) temperature = -temperature;
  if (humidity > 100.0f || temperature < -40.0f || temperature > 80.0f) {
    error = "DHT22 value out of range";
    return false;
  }
  return true;
}

bool readSource(double& value, const char*& error) {
  float t = 0, h = 0;
  if (!readDht(t, h, error)) return false;
  value = FUSEIOT_DHT_QUANTITY == FUSEIOT_DHT_HUMIDITY ? h : t;
  return true;
}

void beginSource() { pinMode(FUSEIOT_DHT_PIN, INPUT_PULLUP); }

String sourceName() {
  return String("DHT22 GPIO") + FUSEIOT_DHT_PIN +
         (FUSEIOT_DHT_QUANTITY == FUSEIOT_DHT_HUMIDITY ? " (humidity)" : " (temperature)");
}
#endif

void poll() {
  double value = 0;
  const char* error = nullptr;
  if (readSource(value, error) && inValidRange(value)) {
    g_raw = value;
    g_haveGood = true;
    g_goodAtMs = millis();
    g_goodAtUnix = fuseiotTimeSynced() ? fuseiotUnixTime() : 0.0;
    g_failures = 0;
    return;
  }
  g_lastError = error ? error : "reading outside FUSEIOT_SENSOR_VALID_MIN/MAX";
  if (++g_failures == 1 || g_failures % 20 == 0) {
    Serial.printf("[sensor] read failed (%lu): %s\n", static_cast<unsigned long>(g_failures), g_lastError);
  }
}

bool fresh() { return g_haveGood && millis() - g_goodAtMs <= FUSEIOT_SENSOR_STALE_MS; }

double roundTo(double v, int decimals) {
  const double f = pow(10.0, decimals);
  return round(v * f) / f;
}

int unavailable(JsonDocument& out) {
  out.clear();
  out["error"] = "sensor read failed";
  out["detail"] = g_lastError;
  return 503;
}

}  // namespace

namespace capability {

void begin() {
  g_prefs.begin("fuseiot", false);
  g_calibrated = g_prefs.getBool("cal", false);
  g_offset = g_calibrated ? g_prefs.getDouble("cal_off", 0.0) : 0.0;
  beginSource();
}

void loop() {
  const uint32_t now = millis();
#if FUSEIOT_SENSOR_SOURCE == FUSEIOT_SENSOR_DHT22
  const bool firstDue = now >= 2000;  // DHT22 needs ~1-2 s after power-up
#else
  const bool firstDue = true;
#endif
  if (!g_polledOnce ? firstDue : now - g_lastPoll >= FUSEIOT_SENSOR_INTERVAL_MS) {
    g_polledOnce = true;
    g_lastPoll = now;
    poll();
  }
}

int handle(const String& path, bool isGet, JsonObjectConst body, JsonDocument& out) {
  if (path == "/reading") {
    if (!isGet) return methodNotAllowed(out, "GET");
    if (!fresh()) return unavailable(out);
    out["value"] = roundTo(g_raw + g_offset, FUSEIOT_SENSOR_DECIMALS);
    out["unit"] = FUSEIOT_SENSOR_UNIT;
    if (g_goodAtUnix > 0) out["timestamp"] = roundTo(g_goodAtUnix, 3);
    out["calibrated"] = g_calibrated;
    return 200;
  }
  if (path == "/calibrate") {
    if (isGet) return methodNotAllowed(out, "POST");
    if (body["reset"].is<bool>() && body["reset"].as<bool>()) {
      g_calibrated = false;
      g_offset = 0.0;
      g_prefs.putBool("cal", false);
      g_prefs.remove("cal_off");
      return replyAccepted(out);
    }
    double reference = 0;
    const Field f = getNumber(body, "reference", reference);
    if (f != Field::Ok) return replyBadField(out, "reference", "must be a number");
    if (!fresh()) return unavailable(out);  // cannot calibrate against nothing
    g_offset = reference - g_raw;
    g_calibrated = true;
    g_prefs.putDouble("cal_off", g_offset);
    g_prefs.putBool("cal", true);
    Serial.printf("[sensor] calibrated: offset %.4f\n", g_offset);
    out["offset"] = g_offset;
    return replyAccepted(out);
  }
  return 0;
}

String describe() {
  String s = sourceName() + ", unit " FUSEIOT_SENSOR_UNIT;
  if (g_calibrated) s += ", calibration offset " + String(g_offset, 4);
  return s;
}

}  // namespace capability

#endif
