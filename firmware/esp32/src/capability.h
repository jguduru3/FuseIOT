// Interface every capability module implements. Exactly one module is
// compiled in, selected by FUSEIOT_CAPABILITY.
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

namespace capability {

// Configure hardware and restore persisted state. Called before WiFi starts.
void begin();

// Non-blocking periodic work (fades, debouncing, sensor sampling...).
void loop();

// Handle one authenticated API request.
//   path:  normalised request path ("/state", "/on", ...)
//   isGet: true for GET, false for POST/PUT
//   body:  JSON object from the request body (empty for GET)
//   out:   response document to fill
// Returns the HTTP status, or 0 if the path is not part of this capability
// (the caller replies 404).
int handle(const String& path, bool isGet, JsonObjectConst body, JsonDocument& out);

// Short human-readable summary for the serial log.
String describe();

}  // namespace capability

// Reply for a known path used with the wrong method.
inline int methodNotAllowed(JsonDocument& out, const char* allowed) {
  out.clear();
  out["error"] = "method not allowed";
  out["allowed"] = allowed;
  return 405;
}
