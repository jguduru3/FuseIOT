// HTTP API: routing, bearer-token auth, JSON parsing and the common endpoints
// (GET / and GET /info). Capability endpoints are delegated to capability::handle.
#pragma once

void apiBegin();
void apiLoop();
bool apiStarted();
