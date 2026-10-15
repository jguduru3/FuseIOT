// FuseIoT ESP32 firmware: implements the FuseIoT HTTP device contract
// (docs/device-contract.md) for one capability chosen at compile time.
#include <Arduino.h>
#include <esp_task_wdt.h>

#include "api.h"
#include "capability.h"
#include "fuseiot_config.h"
#include "net.h"

namespace {

// Task watchdog: the loop must never block this long. 15 s leaves room for a
// slow HTTP client (WebServer waits up to 5 s for data) on top of normal work.
constexpr uint32_t kWatchdogSeconds = 15;

void setupWatchdog() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_config_t cfg = {};
  cfg.timeout_ms = kWatchdogSeconds * 1000;
  cfg.idle_core_mask = 1 << 0;
  cfg.trigger_panic = true;
  esp_task_wdt_reconfigure(&cfg);
#else
  esp_task_wdt_init(kWatchdogSeconds, true);  // re-configures the running TWDT
#endif
  enableLoopWDT();  // the Arduino core feeds it after every loop()
}

}  // namespace

void setup() {
  Serial.begin(FUSEIOT_SERIAL_BAUD);
  Serial.printf("\n[fuseiot] firmware %s, capability %s\n", FUSEIOT_FW_VERSION, FUSEIOT_CAP_NAME);

  capability::begin();  // outputs to a safe/restored state before anything else
  Serial.printf("[fuseiot] %s\n", capability::describe().c_str());

  netBegin();
  setupWatchdog();
}

void loop() {
  netLoop();
  if (netServicesStarted() && !apiStarted()) apiBegin();
  apiLoop();
  capability::loop();
  delay(1);  // yield to the WiFi/IDLE tasks
}
