// CI-safe configuration, selected with -DFUSEIOT_CONFIG_CI (see platformio.ini).
// Lets `pio run` compile every capability without a user config.h.
// Not meant for real devices: the WiFi credentials are placeholders.
#pragma once

#ifndef FUSEIOT_CAPABILITY
#define FUSEIOT_CAPABILITY FUSEIOT_CAP_SWITCHABLE
#endif

#define FUSEIOT_WIFI_SSID "fuseiot-ci"
#define FUSEIOT_WIFI_PASSWORD "fuseiot-ci-password"
#define FUSEIOT_DEVICE_ID ""
// Exercise the auth and PIN code paths in CI builds.
#define FUSEIOT_TOKEN "ci-token"
#define FUSEIOT_OTA_PASSWORD "ci-ota"
#define FUSEIOT_LOCK_PIN_CODE "1234"
#define FUSEIOT_LOCK_AUTO_RELOCK_S 10
#define FUSEIOT_LOCK_SENSE_PIN 25
#define FUSEIOT_BATTERY_ADC_PIN 35
