// Configuration resolution: capability constants, the user's config.h (or the
// CI config) and defaults for every option the user did not set.
#pragma once

#include <Arduino.h>

#define FUSEIOT_FW_VERSION "1.0.0"

// ---- Capability selectors (use these in config.h) ---------------------------
#define FUSEIOT_CAP_SWITCHABLE 1
#define FUSEIOT_CAP_DIMMABLE 2
#define FUSEIOT_CAP_RGB_LIGHT 3
#define FUSEIOT_CAP_SENSOR 4
#define FUSEIOT_CAP_LOCK 5
#define FUSEIOT_CAP_MOTOR 6

#define FUSEIOT_SENSOR_ADC 1
#define FUSEIOT_SENSOR_DHT22 2

#define FUSEIOT_DHT_TEMPERATURE 1
#define FUSEIOT_DHT_HUMIDITY 2

// ---- Load the configuration --------------------------------------------------
#if defined(FUSEIOT_CONFIG_CI)
#include "config.ci.h"
#elif defined(__has_include)
#if __has_include("config.h")
#include "config.h"
#else
#error "FuseIoT: copy include/config.example.h to include/config.h and edit it."
#endif
#else
#include "config.h"
#endif

#ifndef FUSEIOT_CAPABILITY
#error "FuseIoT: set FUSEIOT_CAPABILITY in config.h (e.g. FUSEIOT_CAP_SWITCHABLE)."
#endif

#if FUSEIOT_CAPABILITY == FUSEIOT_CAP_SWITCHABLE
#define FUSEIOT_CAP_NAME "switchable"
#elif FUSEIOT_CAPABILITY == FUSEIOT_CAP_DIMMABLE
#define FUSEIOT_CAP_NAME "dimmable"
#elif FUSEIOT_CAPABILITY == FUSEIOT_CAP_RGB_LIGHT
#define FUSEIOT_CAP_NAME "rgb_light"
#elif FUSEIOT_CAPABILITY == FUSEIOT_CAP_SENSOR
#define FUSEIOT_CAP_NAME "sensor"
#elif FUSEIOT_CAPABILITY == FUSEIOT_CAP_LOCK
#define FUSEIOT_CAP_NAME "lock"
#elif FUSEIOT_CAPABILITY == FUSEIOT_CAP_MOTOR
#define FUSEIOT_CAP_NAME "motor"
#else
#error "FuseIoT: unknown FUSEIOT_CAPABILITY."
#endif

// ---- Target-specific default pins ------------------------------------------
// Only used when config.h does not set the pin. See README.md for wiring.
#if defined(CONFIG_IDF_TARGET_ESP32C3) || defined(CONFIG_IDF_TARGET_ESP32S3)
#define FUSEIOT_DEF_RELAY_PIN 4
#if defined(CONFIG_IDF_TARGET_ESP32C3)
#define FUSEIOT_DEF_BUTTON_PIN 9  // BOOT button
#else
#define FUSEIOT_DEF_BUTTON_PIN 0  // BOOT button
#endif
#define FUSEIOT_DEF_LED_PIN -1
#define FUSEIOT_DEF_PWM_PIN 5
#define FUSEIOT_DEF_RGB_R 5
#define FUSEIOT_DEF_RGB_G 6
#define FUSEIOT_DEF_RGB_B 7
#define FUSEIOT_DEF_ADC_PIN 1
#define FUSEIOT_DEF_DHT_PIN 10
#define FUSEIOT_DEF_REED_PIN 5
#define FUSEIOT_DEF_SERVO_PIN 6
#else  // classic ESP32 (esp32dev)
#define FUSEIOT_DEF_RELAY_PIN 26
#define FUSEIOT_DEF_BUTTON_PIN 0  // BOOT button
#define FUSEIOT_DEF_LED_PIN 2     // on-board LED of most DevKit boards
#define FUSEIOT_DEF_PWM_PIN 25
#define FUSEIOT_DEF_RGB_R 25
#define FUSEIOT_DEF_RGB_G 26
#define FUSEIOT_DEF_RGB_B 27
#define FUSEIOT_DEF_ADC_PIN 34
#define FUSEIOT_DEF_DHT_PIN 4
#define FUSEIOT_DEF_REED_PIN 27
#define FUSEIOT_DEF_SERVO_PIN 18
#endif

// ---- Network / common --------------------------------------------------------
#ifndef FUSEIOT_WIFI_SSID
#error "FuseIoT: set FUSEIOT_WIFI_SSID in config.h."
#endif
#ifndef FUSEIOT_WIFI_PASSWORD
#define FUSEIOT_WIFI_PASSWORD ""
#endif
#ifndef FUSEIOT_DEVICE_ID
#define FUSEIOT_DEVICE_ID ""  // empty: fuseiot-<cap>-<last 3 MAC bytes>
#endif
#ifndef FUSEIOT_TOKEN
#define FUSEIOT_TOKEN ""  // empty: no authentication
#endif
#ifndef FUSEIOT_HTTP_PORT
#define FUSEIOT_HTTP_PORT 80
#endif
#ifndef FUSEIOT_MDNS_ENABLED
#define FUSEIOT_MDNS_ENABLED 1
#endif
#ifndef FUSEIOT_OTA_ENABLED
#define FUSEIOT_OTA_ENABLED 1
#endif
#ifndef FUSEIOT_OTA_PASSWORD
#define FUSEIOT_OTA_PASSWORD ""
#endif
#ifndef FUSEIOT_NTP_SERVER1
#define FUSEIOT_NTP_SERVER1 "pool.ntp.org"
#endif
#ifndef FUSEIOT_NTP_SERVER2
#define FUSEIOT_NTP_SERVER2 "time.google.com"
#endif
#ifndef FUSEIOT_RESTORE_STATE
#define FUSEIOT_RESTORE_STATE 1
#endif
#ifndef FUSEIOT_SERIAL_BAUD
#define FUSEIOT_SERIAL_BAUD 115200
#endif
#ifndef FUSEIOT_WIFI_RETRY_MIN_MS
#define FUSEIOT_WIFI_RETRY_MIN_MS 10000UL
#endif
#ifndef FUSEIOT_WIFI_RETRY_MAX_MS
#define FUSEIOT_WIFI_RETRY_MAX_MS 120000UL
#endif
#ifndef FUSEIOT_WIFI_REBOOT_AFTER_MS
#define FUSEIOT_WIFI_REBOOT_AFTER_MS 0UL  // 0 = never reboot when offline
#endif
#ifndef FUSEIOT_STATE_SAVE_DELAY_MS
#define FUSEIOT_STATE_SAVE_DELAY_MS 2000UL  // coalesce NVS writes (flash wear)
#endif

// ---- switchable --------------------------------------------------------------
#ifndef FUSEIOT_RELAY_PIN
#define FUSEIOT_RELAY_PIN FUSEIOT_DEF_RELAY_PIN
#endif
#ifndef FUSEIOT_RELAY_ACTIVE_LOW
#define FUSEIOT_RELAY_ACTIVE_LOW 0
#endif
#ifndef FUSEIOT_BUTTON_PIN
#define FUSEIOT_BUTTON_PIN FUSEIOT_DEF_BUTTON_PIN  // -1 = no button
#endif
#ifndef FUSEIOT_BUTTON_ACTIVE_LOW
#define FUSEIOT_BUTTON_ACTIVE_LOW 1  // button to GND, internal pull-up
#endif
#ifndef FUSEIOT_BUTTON_DEBOUNCE_MS
#define FUSEIOT_BUTTON_DEBOUNCE_MS 50
#endif
#ifndef FUSEIOT_STATUS_LED_PIN
#define FUSEIOT_STATUS_LED_PIN FUSEIOT_DEF_LED_PIN  // -1 = no LED
#endif
#ifndef FUSEIOT_STATUS_LED_ACTIVE_LOW
#define FUSEIOT_STATUS_LED_ACTIVE_LOW 0
#endif

// ---- dimmable / rgb_light (PWM) ------------------------------------------
#ifndef FUSEIOT_PWM_PIN
#define FUSEIOT_PWM_PIN FUSEIOT_DEF_PWM_PIN
#endif
#ifndef FUSEIOT_RGB_PIN_R
#define FUSEIOT_RGB_PIN_R FUSEIOT_DEF_RGB_R
#endif
#ifndef FUSEIOT_RGB_PIN_G
#define FUSEIOT_RGB_PIN_G FUSEIOT_DEF_RGB_G
#endif
#ifndef FUSEIOT_RGB_PIN_B
#define FUSEIOT_RGB_PIN_B FUSEIOT_DEF_RGB_B
#endif
#ifndef FUSEIOT_RGB_COMMON_ANODE
#define FUSEIOT_RGB_COMMON_ANODE 0
#endif
#ifndef FUSEIOT_PWM_FREQ_HZ
#define FUSEIOT_PWM_FREQ_HZ 5000
#endif
#ifndef FUSEIOT_PWM_RESOLUTION_BITS
#define FUSEIOT_PWM_RESOLUTION_BITS 12
#endif
#ifndef FUSEIOT_PWM_INVERT
#define FUSEIOT_PWM_INVERT 0
#endif
#ifndef FUSEIOT_PWM_GAMMA
#define FUSEIOT_PWM_GAMMA 2.2f  // 1.0 = linear
#endif

// ---- sensor --------------------------------------------------------------
#ifndef FUSEIOT_SENSOR_SOURCE
#define FUSEIOT_SENSOR_SOURCE FUSEIOT_SENSOR_ADC
#endif
#ifndef FUSEIOT_DHT_QUANTITY
#define FUSEIOT_DHT_QUANTITY FUSEIOT_DHT_TEMPERATURE
#endif
#ifndef FUSEIOT_SENSOR_UNIT
#if FUSEIOT_SENSOR_SOURCE == FUSEIOT_SENSOR_DHT22 && FUSEIOT_DHT_QUANTITY == FUSEIOT_DHT_HUMIDITY
#define FUSEIOT_SENSOR_UNIT "percent"
#else
#define FUSEIOT_SENSOR_UNIT "celsius"
#endif
#endif
#ifndef FUSEIOT_ADC_PIN
#define FUSEIOT_ADC_PIN FUSEIOT_DEF_ADC_PIN
#endif
// value = millivolts * SCALE + OFFSET   (defaults: TMP36, 10 mV/degC, 500 mV at 0 degC)
#ifndef FUSEIOT_ADC_SCALE
#define FUSEIOT_ADC_SCALE 0.1
#endif
#ifndef FUSEIOT_ADC_OFFSET
#define FUSEIOT_ADC_OFFSET -50.0
#endif
#ifndef FUSEIOT_ADC_SAMPLES
#define FUSEIOT_ADC_SAMPLES 16
#endif
#ifndef FUSEIOT_DHT_PIN
#define FUSEIOT_DHT_PIN FUSEIOT_DEF_DHT_PIN
#endif
#ifndef FUSEIOT_SENSOR_INTERVAL_MS
#if FUSEIOT_SENSOR_SOURCE == FUSEIOT_SENSOR_DHT22
#define FUSEIOT_SENSOR_INTERVAL_MS 2500UL  // DHT22 needs >= 2 s between reads
#else
#define FUSEIOT_SENSOR_INTERVAL_MS 250UL
#endif
#endif
#ifndef FUSEIOT_SENSOR_STALE_MS
#define FUSEIOT_SENSOR_STALE_MS 10000UL  // no good sample for this long -> 503
#endif
#ifndef FUSEIOT_SENSOR_VALID_MIN
#define FUSEIOT_SENSOR_VALID_MIN -1.0e9
#endif
#ifndef FUSEIOT_SENSOR_VALID_MAX
#define FUSEIOT_SENSOR_VALID_MAX 1.0e9
#endif
#ifndef FUSEIOT_SENSOR_DECIMALS
#define FUSEIOT_SENSOR_DECIMALS 2
#endif

// ---- lock --------------------------------------------------------------------
#ifndef FUSEIOT_LOCK_RELAY_PIN
#define FUSEIOT_LOCK_RELAY_PIN FUSEIOT_DEF_RELAY_PIN
#endif
#ifndef FUSEIOT_LOCK_RELAY_ACTIVE_LOW
#define FUSEIOT_LOCK_RELAY_ACTIVE_LOW 0
#endif
#ifndef FUSEIOT_LOCK_ENERGIZE_TO_UNLOCK
#define FUSEIOT_LOCK_ENERGIZE_TO_UNLOCK 1  // fail-secure strike/solenoid
#endif
#ifndef FUSEIOT_REED_PIN
#define FUSEIOT_REED_PIN FUSEIOT_DEF_REED_PIN  // -1 = no door sensor
#endif
#ifndef FUSEIOT_REED_OPEN_HIGH
#define FUSEIOT_REED_OPEN_HIGH 1  // reed to GND + pull-up: magnet away (open) reads HIGH
#endif
#ifndef FUSEIOT_LOCK_SENSE_PIN
#define FUSEIOT_LOCK_SENSE_PIN -1  // optional bolt-position switch for "jammed"
#endif
#ifndef FUSEIOT_LOCK_SENSE_LOCKED_LOW
#define FUSEIOT_LOCK_SENSE_LOCKED_LOW 1
#endif
#ifndef FUSEIOT_LOCK_JAM_TIMEOUT_MS
#define FUSEIOT_LOCK_JAM_TIMEOUT_MS 3000UL
#endif
#ifndef FUSEIOT_LOCK_PIN_CODE
#define FUSEIOT_LOCK_PIN_CODE ""  // empty: unlock needs no user_code
#endif
#ifndef FUSEIOT_LOCK_MAX_ATTEMPTS
#define FUSEIOT_LOCK_MAX_ATTEMPTS 5
#endif
#ifndef FUSEIOT_LOCK_LOCKOUT_S
#define FUSEIOT_LOCK_LOCKOUT_S 60
#endif
#ifndef FUSEIOT_LOCK_AUTO_RELOCK_S
#define FUSEIOT_LOCK_AUTO_RELOCK_S 0  // 0 = stay unlocked until told
#endif
#ifndef FUSEIOT_LOCK_REFUSE_WHEN_OPEN
#define FUSEIOT_LOCK_REFUSE_WHEN_OPEN 0
#endif
#ifndef FUSEIOT_BATTERY_ADC_PIN
#define FUSEIOT_BATTERY_ADC_PIN -1  // -1: battery_level is null
#endif
#ifndef FUSEIOT_BATTERY_DIVIDER
#define FUSEIOT_BATTERY_DIVIDER 2.0
#endif
#ifndef FUSEIOT_BATTERY_EMPTY_MV
#define FUSEIOT_BATTERY_EMPTY_MV 3300
#endif
#ifndef FUSEIOT_BATTERY_FULL_MV
#define FUSEIOT_BATTERY_FULL_MV 4200
#endif

// ---- motor (hobby servo) -------------------------------------------------
#ifndef FUSEIOT_SERVO_PIN
#define FUSEIOT_SERVO_PIN FUSEIOT_DEF_SERVO_PIN
#endif
#ifndef FUSEIOT_SERVO_MIN_US
#define FUSEIOT_SERVO_MIN_US 500
#endif
#ifndef FUSEIOT_SERVO_MAX_US
#define FUSEIOT_SERVO_MAX_US 2500
#endif
#ifndef FUSEIOT_SERVO_INVERT
#define FUSEIOT_SERVO_INVERT 0
#endif
#ifndef FUSEIOT_MOTOR_POS_MIN
#define FUSEIOT_MOTOR_POS_MIN 0.0f
#endif
#ifndef FUSEIOT_MOTOR_POS_MAX
#define FUSEIOT_MOTOR_POS_MAX 180.0f
#endif
#ifndef FUSEIOT_MOTOR_MAX_DEG_PER_S
#define FUSEIOT_MOTOR_MAX_DEG_PER_S 360.0f  // travel rate at speed 100
#endif
#ifndef FUSEIOT_MOTOR_DEFAULT_SPEED
#define FUSEIOT_MOTOR_DEFAULT_SPEED 50.0f
#endif
#ifndef FUSEIOT_MOTOR_HOME_ON_BOOT
#define FUSEIOT_MOTOR_HOME_ON_BOOT 1
#endif

// ---- Sanity checks ---------------------------------------------------------------
#if FUSEIOT_PWM_RESOLUTION_BITS < 8 || FUSEIOT_PWM_RESOLUTION_BITS > 14
#error "FUSEIOT_PWM_RESOLUTION_BITS must be 8..14 (14 is the C3/S3 maximum)."
#endif
#if FUSEIOT_SENSOR_SOURCE != FUSEIOT_SENSOR_ADC && FUSEIOT_SENSOR_SOURCE != FUSEIOT_SENSOR_DHT22
#error "FUSEIOT_SENSOR_SOURCE must be FUSEIOT_SENSOR_ADC or FUSEIOT_SENSOR_DHT22."
#endif
