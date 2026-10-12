"""Live check for POST /api/capability-settings/{capability}/test against a running backend."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765/api"


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"null")
        except json.JSONDecodeError:
            return error.code, raw.decode("utf-8", "replace")


print("--- 1. demo capability: no network, ok, reachable is null ---")
status, result = call("POST", "/capability-settings/text/test", {"live": False})
assert status == 200, (status, result)
print(status, {k: result[k] for k in ("provider", "configured", "reachable", "ok", "live")})
assert result["reachable"] is None and result["ok"] is True and result["configured"] is True

print("--- 2. unsaved draft pointing at a closed port: reachable false, ok false ---")
status, result = call("POST", "/capability-settings/image/test", {
    "live": False, "provider": "openai", "endpoint": "http://127.0.0.1:9/v1",
    "model": "probe-model", "api_key_env": "OPENAI_API_KEY",
})
assert status == 200, (status, result)
print(status, {k: result[k] for k in ("provider", "model", "configured", "reachable", "ok")})
print("   message:", result["message"][:90])
assert result["reachable"] is False and result["ok"] is False

print("--- 3. image + live=true must NOT perform a real generation ---")
status, result = call("POST", "/capability-settings/image/test", {
    "live": True, "provider": "openai", "endpoint": "http://127.0.0.1:9/v1",
    "model": "probe-model", "api_key_env": "OPENAI_API_KEY",
})
assert status == 200, (status, result)
print(status, "live =", result["live"])
print("   message:", result["message"][:120])
assert "未做真实生成测试" in result["message"]

print("--- 4. the draft must not have been persisted ---")
status, saved = call("GET", "/capability-settings/image")
print(status, {k: saved[k] for k in ("provider", "endpoint", "model")})
assert saved["provider"] == "demo" and saved["endpoint"] == ""

print("--- 5. validation errors ---")
print("unknown capability ->", call("POST", "/capability-settings/video/test", {"live": False})[0])
print("bad api_key_env   ->", call("POST", "/capability-settings/text/test", {"api_key_env": "bad-name"})[0])
assert call("POST", "/capability-settings/video/test", {"live": False})[0] == 404
assert call("POST", "/capability-settings/text/test", {"api_key_env": "bad-name"})[0] == 422

print("LIVE_PROBE_OK")
