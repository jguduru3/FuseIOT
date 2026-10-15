#include "api.h"

#include <ArduinoJson.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_timer.h>

#include "capability.h"
#include "fuseiot_config.h"
#include "util.h"

namespace {

WebServer g_server(FUSEIOT_HTTP_PORT);
bool g_started = false;

constexpr size_t kMaxBodyBytes = 2048;

void send(int status, const JsonDocument& doc) {
  String payload;
  serializeJson(doc, payload);
  g_server.sendHeader("Cache-Control", "no-store");
  g_server.send(status, "application/json", payload);
}

void sendError(int status, const char* message) {
  JsonDocument doc;
  doc["error"] = message;
  send(status, doc);
}

// "Authorization: Bearer <token>"; scheme is case-insensitive.
bool authorized() {
  static const size_t tokenLen = strlen(FUSEIOT_TOKEN);
  if (tokenLen == 0) return true;
  String header = g_server.header("Authorization");
  header.trim();
  if (header.length() < 7 || !header.substring(0, 7).equalsIgnoreCase("bearer ")) {
    // Still run a comparison so a missing header costs the same as a wrong one.
    fuseiotSecureEquals("", 0, FUSEIOT_TOKEN, tokenLen);
    return false;
  }
  String given = header.substring(7);
  given.trim();
  return fuseiotSecureEquals(given.c_str(), given.length(), FUSEIOT_TOKEN, tokenLen);
}

String normalisedPath() {
  String path = g_server.uri();
  int q = path.indexOf('?');
  if (q >= 0) path.remove(q);
  while (path.length() > 1 && path.endsWith("/")) path.remove(path.length() - 1);
  if (path.length() == 0) path = "/";
  return path;
}

void handleRoot() {
  JsonDocument doc;
  doc["device_type"] = FUSEIOT_CAP_NAME;
  doc["id"] = fuseiotDeviceId();
  doc["fw"] = FUSEIOT_FW_VERSION;
  doc["status"] = "ok";
  send(200, doc);
}

void handleInfo() {
  JsonDocument doc;
  doc["id"] = fuseiotDeviceId();
  doc["cap"] = FUSEIOT_CAP_NAME;
  doc["fw"] = FUSEIOT_FW_VERSION;
  doc["uptime_s"] = static_cast<uint32_t>(esp_timer_get_time() / 1000000ULL);
  doc["rssi"] = WiFi.RSSI();
  doc["free_heap"] = ESP.getFreeHeap();
  doc["ip"] = WiFi.localIP().toString();
  doc["time_synced"] = fuseiotTimeSynced();
  doc["auth"] = strlen(FUSEIOT_TOKEN) > 0;
  doc["chip"] = ESP.getChipModel();
  send(200, doc);
}

void handleRequest() {
  const HTTPMethod method = g_server.method();
  const bool isGet = method == HTTP_GET;
  const bool isWrite = method == HTTP_POST || method == HTTP_PUT;
  const String path = normalisedPath();

  // Reachability probe: always open, never 401.
  if (path == "/" && isGet) {
    handleRoot();
    return;
  }
  if (!authorized()) {
    sendError(401, "unauthorized");
    return;
  }
  if (!isGet && !isWrite) {
    sendError(405, "method not allowed");
    return;
  }

  JsonDocument request;
  JsonObjectConst body;
  if (isWrite && g_server.hasArg("plain")) {
    const String& raw = g_server.arg("plain");
    if (raw.length() > kMaxBodyBytes) {
      sendError(413, "request body too large");
      return;
    }
    bool blank = true;
    for (size_t i = 0; i < raw.length(); i++) {
      if (!isspace(static_cast<unsigned char>(raw[i]))) {
        blank = false;
        break;
      }
    }
    if (!blank) {
      DeserializationError err = deserializeJson(request, raw);
      if (err) {
        JsonDocument doc;
        doc["error"] = String("invalid json: ") + err.c_str();
        send(400, doc);
        return;
      }
      if (!request.is<JsonObject>()) {
        sendError(400, "request body must be a JSON object");
        return;
      }
    }
  }
  if (request.is<JsonObject>()) {
    body = request.as<JsonObjectConst>();
  } else {
    body = request.to<JsonObject>();  // empty object for GET / empty body
  }

  if (path == "/info") {
    if (isGet) {
      handleInfo();
    } else {
      sendError(405, "method not allowed");
    }
    return;
  }

  JsonDocument out;
  int status = capability::handle(path, isGet, body, out);
  if (status == 0) {
    out.clear();
    out["error"] = "not found";
    out["path"] = path;
    status = 404;
  }
  send(status, out);
}

}  // namespace

void apiBegin() {
  static const char* headers[] = {"Authorization"};
  g_server.collectHeaders(headers, 1);
  g_server.onNotFound(handleRequest);  // single dispatcher for every path
  g_server.begin();
  g_started = true;
  Serial.printf("[http] listening on port %d (auth %s)\n", FUSEIOT_HTTP_PORT,
                strlen(FUSEIOT_TOKEN) > 0 ? "on" : "off");
}

void apiLoop() {
  if (g_started) g_server.handleClient();
}

bool apiStarted() { return g_started; }
