// Thin LEDC wrapper that compiles on Arduino-ESP32 core 2.x and 3.x.
#pragma once

#include <Arduino.h>

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
#define FUSEIOT_LEDC_V3 1
#else
#define FUSEIOT_LEDC_V3 0
#endif

class PwmOut {
 public:
  // channel is only used on core 2.x; core 3.x allocates channels itself.
  bool begin(int pin, uint8_t channel, uint32_t freqHz, uint8_t bits, bool invert = false) {
    if (pin < 0) return false;
    pin_ = pin;
    channel_ = channel;
    invert_ = invert;
    max_ = (1UL << bits) - 1;
#if FUSEIOT_LEDC_V3
    ok_ = ledcAttachChannel(pin, freqHz, bits, channel);
#else
    ok_ = ledcSetup(channel, freqHz, bits) != 0;
    if (ok_) ledcAttachPin(pin, channel);
#endif
    write(0);
    return ok_;
  }

  // Raw duty, 0..maxDuty().
  void write(uint32_t duty) {
    if (!ok_) return;
    if (duty > max_) duty = max_;
    if (invert_) duty = max_ - duty;
#if FUSEIOT_LEDC_V3
    ledcWrite(pin_, duty);
#else
    ledcWrite(channel_, duty);
#endif
  }

  // Fraction 0..1 of full scale.
  void writeFraction(float f) {
    if (f <= 0.0f) {
      write(0);
    } else if (f >= 1.0f) {
      write(max_);
    } else {
      write(static_cast<uint32_t>(f * static_cast<float>(max_) + 0.5f));
    }
  }

  uint32_t maxDuty() const { return max_; }
  bool ok() const { return ok_; }

 private:
  int pin_ = -1;
  uint8_t channel_ = 0;
  bool invert_ = false;
  bool ok_ = false;
  uint32_t max_ = 255;
};

// Perceptual brightness: maps 0..1 to a PWM fraction with the configured gamma.
inline float fuseiotGamma(float level, float gamma) {
  if (level <= 0.0f) return 0.0f;
  if (level >= 1.0f) return 1.0f;
  return gamma == 1.0f ? level : powf(level, gamma);
}
