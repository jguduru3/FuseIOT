#include "net.h"

#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <WiFi.h>

#include "fuseiot_config.h"
#include "util.h"

namespace {

String g_id;
bool g_services = false;
bool g_wasConnected = false;
uint32_t g_lastAttempt = 0;
uint32_t g_retryDelay = FUSEIOT_WIFI_RETRY_MIN_MS;
uint32_t g_offlineSince = 0;

// mDNS hostnames: lowercase letters, digits and '-', at most 63 characters.
String sanitizeHostname(const String& in) {
  String out;
  out.reserve(in.length());
  for (size_t i = 0; i < in.length() && out.length() < 63; i++) {
    char c = static_cast<char>(tolower(static_cast<unsigned char>(in[i])));
    bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-';
    out += ok ? c : '-';
  }
  while (out.startsWith("-")) out.remove(0, 1);
  while (out.endsWith("-")) out.remove(out.length() - 1);
  return out;
}

String defaultDeviceId() {
  uint64_t mac = ESP.getEfuseMac();  // byte 0 is the first MAC octet
  char suffix[7];
  snprintf(suffix, sizeof(suffix), "%02x%02x%02x", static_cast<unsigned>((mac >> 24) & 0xFF),
           static_cast<unsigned>((mac >> 32) & 0xFF), static_cast<unsigned>((mac >> 40) & 0xFF));
  String id = "fuseiot-" FUSEIOT_CAP_NAME "-";
  id += suffix;
  return sanitizeHostname(id);  // rgb_light -> rgb-light
}

void startWifiAttempt() {
  WiFi.disconnect();
  WiFi.begin(FUSEIOT_WIFI_SSID, FUSEIOT_WIFI_PASSWORD);
  g_lastAttempt = millis();
}

void startServices() {
  const char* id = g_id.c_str();

  configTime(0, 0, FUSEIOT_NTP_SERVER1, FUSEIOT_NTP_SERVER2);

#if FUSEIOT_OTA_ENABLED
  ArduinoOTA.setHostname(id);
  if (strlen(FUSEIOT_OTA_PASSWORD) > 0) ArduinoOTA.setPassword(FUSEIOT_OTA_PASSWORD);
#if !FUSEIOT_MDNS_ENABLED
  ArduinoOTA.setMdnsEnabled(false);
#endif
  ArduinoOTA.onStart([]() {
    // An upload runs inside ArduinoOTA.handle() for many seconds.
    disableLoopWDT();
    Serial.println(F("[ota] update started"));
  });
  ArduinoOTA.onEnd([]() { Serial.println(F("[ota] update finished, rebooting")); });
  ArduinoOTA.onError([](ota_error_t error) {
    Serial.printf("[ota] error %u\n", static_cast<unsigned>(error));
    enableLoopWDT();
  });
  ArduinoOTA.begin();  // also starts mDNS with our hostname when enabled
#elif FUSEIOT_MDNS_ENABLED
  if (!MDNS.begin(id)) Serial.println(F("[mdns] failed to start"));
#endif

#if FUSEIOT_MDNS_ENABLED
  // `fuseiot discover` reads exactly these TXT keys.
  MDNS.addService("fuseiot", "tcp", FUSEIOT_HTTP_PORT);
  MDNS.addServiceTxt("fuseiot", "tcp", "id", id);
  MDNS.addServiceTxt("fuseiot", "tcp", "cap", FUSEIOT_CAP_NAME);
  MDNS.addServiceTxt("fuseiot", "tcp", "fw", FUSEIOT_FW_VERSION);
  MDNS.addServiceTxt("fuseiot", "tcp", "auth", strlen(FUSEIOT_TOKEN) > 0 ? "1" : "0");
  Serial.printf("[mdns] http://%s.local:%d  (_fuseiot._tcp)\n", id, FUSEIOT_HTTP_PORT);
#endif

  g_services = true;
}

}  // namespace

const String& fuseiotDeviceId() { return g_id; }

bool netServicesStarted() { return g_services; }

void netBegin() {
  g_id = strlen(FUSEIOT_DEVICE_ID) > 0 ? sanitizeHostname(FUSEIOT_DEVICE_ID) : defaultDeviceId();
  if (g_id.length() == 0) g_id = defaultDeviceId();
  Serial.printf("[net] device id %s, connecting to \"%s\"\n", g_id.c_str(), FUSEIOT_WIFI_SSID);

  WiFi.persistent(false);       // don't rewrite WiFi credentials to flash on every boot
  WiFi.setHostname(g_id.c_str());  // must precede mode()/begin()
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);  // reconnects are handled below, with backoff
  WiFi.setSleep(false);          // modem sleep adds 100+ ms to every request
  startWifiAttempt();
  g_offlineSince = millis();
}

void netLoop() {
  const uint32_t now = millis();
  const bool connected = WiFi.status() == WL_CONNECTED;

  if (connected) {
    if (!g_wasConnected) {
      g_wasConnected = true;
      g_retryDelay = FUSEIOT_WIFI_RETRY_MIN_MS;
      Serial.printf("[net] connected, ip %s, rssi %d dBm\n", WiFi.localIP().toString().c_str(),
                    WiFi.RSSI());
      if (!g_services) startServices();
    }
  } else {
    if (g_wasConnected) {
      g_wasConnected = false;
      g_offlineSince = now;
      g_lastAttempt = now - g_retryDelay;  // retry immediately after a drop
      Serial.println(F("[net] WiFi lost"));
    }
    if (now - g_lastAttempt >= g_retryDelay) {
      startWifiAttempt();
      g_retryDelay = g_retryDelay * 2 > FUSEIOT_WIFI_RETRY_MAX_MS ? FUSEIOT_WIFI_RETRY_MAX_MS
                                                                  : g_retryDelay * 2;
      Serial.printf("[net] connecting... (next retry in %lu s)\n",
                    static_cast<unsigned long>(g_retryDelay / 1000));
    }
#if FUSEIOT_WIFI_REBOOT_AFTER_MS > 0
    if (now - g_offlineSince >= FUSEIOT_WIFI_REBOOT_AFTER_MS) {
      Serial.println(F("[net] offline too long, rebooting"));
      ESP.restart();
    }
#endif
  }

#if FUSEIOT_OTA_ENABLED
  if (g_services) ArduinoOTA.handle();
#endif
}
