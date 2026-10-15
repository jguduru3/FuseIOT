// WiFi (STA with reconnect backoff), mDNS advertisement, ArduinoOTA and NTP.
#pragma once

#include <Arduino.h>

// Derives the device id and starts the WiFi connection (non-blocking).
void netBegin();

// Call every loop(): reconnects with backoff, starts network services after
// the first connection, and services OTA.
void netLoop();

// True after WiFi connected at least once and the HTTP server may run.
bool netServicesStarted();
