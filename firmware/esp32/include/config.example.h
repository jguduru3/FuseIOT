// FuseIoT ESP32 firmware configuration.
//
// Copy this file to include/config.h and edit it. config.h is git-ignored so
// your WiFi password and token stay out of version control.
//
// Every commented-out option shows its default. Pin defaults depend on the
// chip (ESP32 / ESP32-C3 / ESP32-S3); see the wiring table in README.md.
#pragma once

// ============================================================================
// 1. What this board is
// ============================================================================
// One of: FUSEIOT_CAP_SWITCHABLE, FUSEIOT_CAP_DIMMABLE, FUSEIOT_CAP_RGB_LIGHT,
//         FUSEIOT_CAP_SENSOR, FUSEIOT_CAP_LOCK, FUSEIOT_CAP_MOTOR
#ifndef FUSEIOT_CAPABILITY
#define FUSEIOT_CAPABILITY FUSEIOT_CAP_SWITCHABLE
#endif

// ============================================================================
// 2. Network and identity
// ============================================================================
#define FUSEIOT_WIFI_SSID "your-wifi"
#define FUSEIOT_WIFI_PASSWORD "your-wifi-password"

// mDNS hostname and FuseIoT id (letters, digits, '-'). Empty = automatic:
// fuseiot-<capability>-<last 3 MAC bytes>, e.g. fuseiot-switchable-a1b2c3.
#define FUSEIOT_DEVICE_ID ""

// Bearer token. When set, every endpoint except GET / requires
// "Authorization: Bearer <token>". Strongly recommended for locks.
#define FUSEIOT_TOKEN ""

// Password for over-the-air updates ("" = no password; not recommended).
#define FUSEIOT_OTA_PASSWORD "change-me-ota"

// #define FUSEIOT_HTTP_PORT 80
// #define FUSEIOT_MDNS_ENABLED 1
// #define FUSEIOT_OTA_ENABLED 1
// #define FUSEIOT_NTP_SERVER1 "pool.ntp.org"
// #define FUSEIOT_NTP_SERVER2 "time.google.com"
// #define FUSEIOT_RESTORE_STATE 1          // restore last state after power loss
// #define FUSEIOT_WIFI_REBOOT_AFTER_MS 0   // reboot if offline this long (0 = never)

// ============================================================================
// 3. Capability options (only the block for your capability matters)
// ============================================================================

// ---- switchable: relay + optional push button + optional status LED ----------
// #define FUSEIOT_RELAY_PIN 26
// #define FUSEIOT_RELAY_ACTIVE_LOW 0       // 1 for most "low-level trigger" relay boards
// #define FUSEIOT_BUTTON_PIN 0             // -1 = no button (0 = BOOT button)
// #define FUSEIOT_BUTTON_ACTIVE_LOW 1      // button between pin and GND
// #define FUSEIOT_BUTTON_DEBOUNCE_MS 50
// #define FUSEIOT_STATUS_LED_PIN 2         // -1 = none; follows the relay
// #define FUSEIOT_STATUS_LED_ACTIVE_LOW 0

// ---- dimmable: one PWM output (LED strip via MOSFET, LED driver PWM input) ----
// #define FUSEIOT_PWM_PIN 25
// #define FUSEIOT_PWM_FREQ_HZ 5000
// #define FUSEIOT_PWM_RESOLUTION_BITS 12   // 8..14
// #define FUSEIOT_PWM_INVERT 0
// #define FUSEIOT_PWM_GAMMA 2.2f           // perceptual curve; 1.0f = linear

// ---- rgb_light: three PWM outputs (uses the PWM options above too) -----------
// #define FUSEIOT_RGB_PIN_R 25
// #define FUSEIOT_RGB_PIN_G 26
// #define FUSEIOT_RGB_PIN_B 27
// #define FUSEIOT_RGB_COMMON_ANODE 0       // 1 for a common-anode LED wired directly

// ---- sensor --------------------------------------------------------------------
// Source: FUSEIOT_SENSOR_ADC (analog probe) or FUSEIOT_SENSOR_DHT22
// #define FUSEIOT_SENSOR_SOURCE FUSEIOT_SENSOR_ADC
// #define FUSEIOT_SENSOR_UNIT "celsius"
//
// ADC: value = millivolts * SCALE + OFFSET
//   TMP36:  SCALE 0.1, OFFSET -50.0  (default)
//   LM35:   SCALE 0.1, OFFSET 0.0
//   Soil probe as 0-100 %: e.g. SCALE -0.04, OFFSET 120.0 (calibrate dry/wet)
// #define FUSEIOT_ADC_PIN 34               // must be an ADC1 pin (ADC2 is unusable with WiFi)
// #define FUSEIOT_ADC_SCALE 0.1
// #define FUSEIOT_ADC_OFFSET -50.0
// #define FUSEIOT_ADC_SAMPLES 16
//
// DHT22 / AM2302 (data pin needs a 4.7k-10k pull-up to 3.3 V):
// #define FUSEIOT_DHT_PIN 4
// #define FUSEIOT_DHT_QUANTITY FUSEIOT_DHT_TEMPERATURE   // or FUSEIOT_DHT_HUMIDITY ("percent")
//
// #define FUSEIOT_SENSOR_INTERVAL_MS 250   // 2500 for DHT22
// #define FUSEIOT_SENSOR_STALE_MS 10000    // no good sample this long -> HTTP 503
// #define FUSEIOT_SENSOR_VALID_MIN -1.0e9  // readings outside -> HTTP 503
// #define FUSEIOT_SENSOR_VALID_MAX 1.0e9
// #define FUSEIOT_SENSOR_DECIMALS 2

// ---- lock: lock relay/MOSFET + optional reed switch ----------------------------
// #define FUSEIOT_LOCK_RELAY_PIN 26
// #define FUSEIOT_LOCK_RELAY_ACTIVE_LOW 0
// #define FUSEIOT_LOCK_ENERGIZE_TO_UNLOCK 1   // 0 for fail-safe (energize to lock)
// #define FUSEIOT_REED_PIN 27                 // -1 = no door sensor
// #define FUSEIOT_REED_OPEN_HIGH 1            // reed to GND with pull-up: open reads HIGH
// #define FUSEIOT_LOCK_SENSE_PIN -1           // bolt switch; enables "jammed" detection
// #define FUSEIOT_LOCK_SENSE_LOCKED_LOW 1
// #define FUSEIOT_LOCK_JAM_TIMEOUT_MS 3000
// #define FUSEIOT_LOCK_PIN_CODE ""            // set to require user_code to unlock
// #define FUSEIOT_LOCK_MAX_ATTEMPTS 5         // wrong codes before lockout
// #define FUSEIOT_LOCK_LOCKOUT_S 60
// #define FUSEIOT_LOCK_AUTO_RELOCK_S 0        // relock N s after unlock (0 = off)
// #define FUSEIOT_LOCK_REFUSE_WHEN_OPEN 0     // 1: refuse to lock while the door is open
// #define FUSEIOT_BATTERY_ADC_PIN -1          // -1: battery_level is null
// #define FUSEIOT_BATTERY_DIVIDER 2.0
// #define FUSEIOT_BATTERY_EMPTY_MV 3300
// #define FUSEIOT_BATTERY_FULL_MV 4200

// ---- motor: hobby servo --------------------------------------------------------
// #define FUSEIOT_SERVO_PIN 18
// #define FUSEIOT_SERVO_MIN_US 500            // pulse at FUSEIOT_MOTOR_POS_MIN
// #define FUSEIOT_SERVO_MAX_US 2500           // pulse at FUSEIOT_MOTOR_POS_MAX
// #define FUSEIOT_SERVO_INVERT 0
// #define FUSEIOT_MOTOR_POS_MIN 0.0f
// #define FUSEIOT_MOTOR_POS_MAX 180.0f
// #define FUSEIOT_MOTOR_MAX_DEG_PER_S 360.0f  // travel rate at speed 100
// #define FUSEIOT_MOTOR_DEFAULT_SPEED 50.0f
// #define FUSEIOT_MOTOR_HOME_ON_BOOT 1        // drive to POS_MIN at boot (homed = true)
