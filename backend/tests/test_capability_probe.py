"""Capability "test connection": configuration, reachability, and live-call honesty.

Every test runs against a real loopback HTTP server (``ThreadingHTTPServer`` on
``127.0.0.1:0``), so the reachability probe, the request path, the headers, and the JSON body
are observed exactly as the probe sends them. A closed-port check uses a port this process
just proved is free, so "unreachable" is a genuine refusal rather than a mock's opinion.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app

TEST_API_KEY = "sk-test-secret-vmaker-456"
TEST_KEY_ENV = "VMAKER_TEST_PROBE_KEY"


class ProbeMockHandler(BaseHTTPRequestHandler):
    """Records every HTTP request and answers from the routing table the test installed."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - silence the test log
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._serve()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._serve()

    @property
    def mock(self) -> "ProbeMock":
        return self.server.mock  # type: ignore[attr-defined]

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.mock.record({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "body": raw,
        })
        entry = self.mock.routes.get(self.path)
        if entry is None:
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        status, payload, content_type = entry
        self._send(status, payload, content_type)

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ProbeMock:
    """A loopback stand-in for an OpenAI-compatible text/speech service.

    Only real HTTP requests are recorded: the TCP reachability probe finishes its handshake
    and closes without ever reaching this handler, which is exactly what the tests assert.
    """

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes, str]] = {}
        self.log: list[dict] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeMockHandler)
        self.server.mock = self  # type: ignore[attr-defined]
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def record(self, entry: dict) -> None:
        with self.lock:
            self.log.append(entry)

    def json_route(self, path: str, payload: dict, status: int = 200) -> None:
        self.routes[path] = (status, json.dumps(payload).encode("utf-8"), "application/json")

    def error_route(self, path: str, status: int, message: str) -> None:
        self.json_route(path, {"error": {"message": message}}, status=status)

    def requests(self, path: str | None = None) -> list[dict]:
        entries = list(self.log)
        return [entry for entry in entries if entry["path"] == path] if path else entries


@pytest.fixture
def mock():
    server = ProbeMock()
    try:
        yield server
    finally:
        server.close()


# --- helpers ------------------------------------------------------------


def _set_capability(client: TestClient, capability: str, **payload) -> dict:
    response = client.put(f"/api/capability-settings/{capability}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _external(client: TestClient, capability: str, endpoint: str, *, model: str = "mock-model-v1",
              api_key_env: str = TEST_KEY_ENV) -> dict:
    return _set_capability(client, capability, provider="openai", endpoint=endpoint, model=model,
                           api_key_env=api_key_env, unit_cost=0.0)


def _test(client: TestClient, capability: str, **body) -> dict:
    response = client.post(f"/api/capability-settings/{capability}/test", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _body(entry: dict) -> dict:
    return json.loads(entry["body"].decode("utf-8"))


def _closed_port() -> int:
    """A port this process just bound and released, so nothing can be listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# --- 1. demo capability -------------------------------------------------


def test_demo_capability_needs_no_network_and_makes_no_request(tmp_path: Path, mock) -> None:
    with TestClient(create_app(tmp_path / "probe_demo.db")) as client:
        _set_capability(client, "image", provider="demo", endpoint=mock.url,
                        model="demo-svg-v1", api_key_env="", unit_cost=0)
        result = _test(client, "image", live=True)
        assert result["capability"] == "image"
        assert result["provider"] == "demo"
        assert result["configured"] is True
        assert result["reachable"] is None          # a local generator has no endpoint to reach
        assert result["ok"] is True
        assert result["live"] is False              # nothing billable was ever attempted
        assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
        assert "demo" in result["message"] or "占位" in result["message"]
        # A demo capability must not touch the network, even when its row holds a real URL.
        assert mock.requests() == []


# --- 2. missing environment variable ------------------------------------


def test_missing_api_key_env_is_reported_without_the_secret(tmp_path: Path, mock,
                                                            monkeypatch) -> None:
    monkeypatch.delenv(TEST_KEY_ENV, raising=False)
    # Another credential present in the process must not be echoed into the message either.
    monkeypatch.setenv("OTHER_SERVICE_KEY", TEST_API_KEY)
    with TestClient(create_app(tmp_path / "probe_nokey.db")) as client:
        saved = _external(client, "text", mock.url, model="mock-text-v1")
        assert saved["configured"] is False
        result = _test(client, "text", live=True)
        assert result["configured"] is False
        assert result["ok"] is False
        assert result["live"] is False
        # The variable name is the whole point of the message; the value is never known here.
        assert TEST_KEY_ENV in result["message"]
        assert "密钥未配置" in result["message"]
        assert TEST_API_KEY not in result["message"]
        assert mock.requests() == []


# --- 3. missing endpoint ------------------------------------------------


def test_missing_endpoint_is_reachable_false_with_an_explicit_reason(tmp_path: Path,
                                                                     monkeypatch) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    with TestClient(create_app(tmp_path / "probe_noendpoint.db")) as client:
        _set_capability(client, "speech", provider="openai", endpoint="", model="mock-speech-v1",
                        api_key_env=TEST_KEY_ENV, unit_cost=0)
        result = _test(client, "speech", live=True)
        assert result["configured"] is True
        assert result["reachable"] is False
        assert result["ok"] is False
        assert result["live"] is False
        assert "endpoint" in result["message"]


# --- 4. closed port -----------------------------------------------------


def test_endpoint_on_a_closed_port_is_unreachable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    endpoint = f"http://127.0.0.1:{_closed_port()}"
    with TestClient(create_app(tmp_path / "probe_closed.db")) as client:
        _external(client, "text", endpoint)
        result = _test(client, "text", live=True)
        assert result["configured"] is True
        assert result["reachable"] is False
        assert result["ok"] is False
        assert result["live"] is False
        assert "无法连接" in result["message"]


# --- 5. reachable but not live ------------------------------------------


def test_reachable_endpoint_without_live_makes_no_generation_request(tmp_path: Path, mock,
                                                                    monkeypatch) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    with TestClient(create_app(tmp_path / "probe_passive.db")) as client:
        _external(client, "text", mock.url, model="mock-text-v1")
        result = _test(client, "text", live=False)
        assert result["configured"] is True
        assert result["reachable"] is True
        assert result["ok"] is True
        assert result["live"] is False
        assert "TCP" in result["message"]
        assert "未发送任何鉴权请求" in result["message"]
        # Reachability is a bare TCP connect: it never becomes an HTTP request at the service.
        assert mock.requests() == []


# --- 6. live text call --------------------------------------------------


def test_live_text_call_posts_the_minimal_chat_request(tmp_path: Path, mock, monkeypatch) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock.json_route("/chat/completions", {"choices": [{"message": {"content": "pong"}}]})
    with TestClient(create_app(tmp_path / "probe_live_text.db")) as client:
        _external(client, "text", mock.url, model="mock-text-v1")
        result = _test(client, "text", live=True)
        assert result["provider"] == "openai" and result["model"] == "mock-text-v1"
        assert result["configured"] is True and result["reachable"] is True
        assert result["live"] is True
        assert result["ok"] is True
        assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0

        posts = mock.requests("/chat/completions")
        assert len(posts) == 1, posts
        assert posts[0]["method"] == "POST"
        assert posts[0]["authorization"] == f"Bearer {TEST_API_KEY}"
        payload = _body(posts[0])
        assert payload["model"] == "mock-text-v1"
        assert payload["max_tokens"] == 1
        assert payload["messages"] == [{"role": "user", "content": "ping"}]


def test_live_speech_call_posts_the_minimal_audio_request(tmp_path: Path, mock,
                                                          monkeypatch) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock.routes["/audio/speech"] = (200, b"RIFF\x00\x00\x00\x00WAVE", "audio/wav")
    with TestClient(create_app(tmp_path / "probe_live_speech.db")) as client:
        _external(client, "speech", mock.url, model="mock-speech-v1")
        result = _test(client, "speech", live=True)
        assert result["live"] is True and result["ok"] is True
        posts = mock.requests("/audio/speech")
        assert len(posts) == 1, posts
        assert posts[0]["authorization"] == f"Bearer {TEST_API_KEY}"
        payload = _body(posts[0])
        assert payload["model"] == "mock-speech-v1"
        assert payload["input"] == "。"
        assert payload["response_format"] == "wav"


# --- 7. live text call failing upstream ---------------------------------


def test_live_text_call_http_error_fails_without_leaking_the_key(tmp_path: Path, mock,
                                                                 monkeypatch) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    # The upstream error body echoes the credential: it must never reach the response.
    mock.error_route("/chat/completions", 500, f"upstream exploded for key {TEST_API_KEY}")
    with TestClient(create_app(tmp_path / "probe_live_500.db")) as client:
        _external(client, "text", mock.url)
        result = _test(client, "text", live=True)
        assert result["reachable"] is True      # the socket did answer...
        assert result["live"] is True           # ...and a real call was attempted
        assert result["ok"] is False            # ...but the service rejected it
        assert "500" in result["message"]
        assert TEST_API_KEY not in result["message"]
        assert "upstream exploded" not in result["message"]
        assert len(mock.requests("/chat/completions")) == 1


def test_live_call_never_echoes_a_credential_the_service_quotes_back(tmp_path: Path, mock,
                                                                     monkeypatch) -> None:
    """Even when the upstream error body quotes the key, the response must not carry it."""
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock.json_route("/chat/completions",
                    {"error": {"message": f"invalid key {TEST_API_KEY}",
                               "authorization": f"Bearer {TEST_API_KEY}"}}, status=401)
    with TestClient(create_app(tmp_path / "probe_scrub.db")) as client:
        _external(client, "text", mock.url)
        result = _test(client, "text", live=True)
        assert result["ok"] is False
        blob = json.dumps(result, ensure_ascii=False)
        assert TEST_API_KEY not in blob
        assert "401" in result["message"]


# --- 8. image never tests generation ------------------------------------

def test_live_image_request_never_generates_an_image(tmp_path: Path, mock, monkeypatch) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    with TestClient(create_app(tmp_path / "probe_live_image.db")) as client:
        _external(client, "image", mock.url, model="mock-image-v1")
        result = _test(client, "image", live=True)
        assert result["live"] is False
        assert result["configured"] is True and result["reachable"] is True
        assert result["ok"] is True             # ok reflects configured + reachable only
        assert "图像能力未做真实生成测试" in result["message"]
        assert "费用" in result["message"]
        assert mock.requests() == []


# --- 9. testing an unsaved configuration --------------------------------


def test_unsaved_configuration_is_tested_without_writing_the_row(tmp_path: Path, mock,
                                                                 monkeypatch) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock.json_route("/chat/completions", {"choices": [{"message": {"content": "pong"}}]})
    with TestClient(create_app(tmp_path / "probe_draft.db")) as client:
        _external(client, "text", "https://api.example.invalid/v1", model="saved-model")
        before = client.get("/api/capability-settings/text").json()

        result = _test(client, "text", live=True, provider="openai", endpoint=mock.url,
                       model="draft-model", api_key_env=TEST_KEY_ENV)
        # The response reports the draft configuration that was actually exercised.
        assert result["provider"] == "openai"
        assert result["model"] == "draft-model"
        assert result["live"] is True and result["ok"] is True
        payload = _body(mock.requests("/chat/completions")[0])
        assert payload["model"] == "draft-model"

        # Testing must never persist anything: the saved row is byte-for-byte unchanged.
        assert client.get("/api/capability-settings/text").json() == before
        assert client.get("/api/capability-settings/text").json()["model"] == "saved-model"


def test_omitted_fields_fall_back_to_the_saved_row(tmp_path: Path, mock, monkeypatch) -> None:
    """A partial body tests the saved endpoint rather than inventing one."""
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock.json_route("/chat/completions", {"choices": [{"message": {"content": "pong"}}]})
    with TestClient(create_app(tmp_path / "probe_partial.db")) as client:
        _external(client, "text", mock.url, model="saved-model")
        result = _test(client, "text", live=True)
        assert result["model"] == "saved-model"
        assert result["reachable"] is True and result["ok"] is True
        assert len(mock.requests("/chat/completions")) == 1


# --- 10. request validation ---------------------------------------------


def test_capability_test_request_validation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(TEST_KEY_ENV, raising=False)
    with TestClient(create_app(tmp_path / "probe_validation.db")) as client:
        # An unknown capability is a 404, both with and without a body.
        assert client.post("/api/capability-settings/audio/test", json={"live": True}).status_code == 404
        assert client.post("/api/capability-settings/audio/test").status_code == 404
        # The PUT validation rules are reused verbatim for a test-only configuration.
        assert client.post("/api/capability-settings/text/test",
                           json={"provider": "openai", "api_key_env": "lower-case"}).status_code == 422
        assert client.post("/api/capability-settings/text/test",
                           json={"provider": "  ", "model": "m"}).status_code == 422
        assert client.post("/api/capability-settings/text/test",
                           json={"model": ""}).status_code == 422
        # A valid draft passes, and an empty body tests the saved row.
        assert client.post("/api/capability-settings/text/test",
                           json={"provider": "openai", "endpoint": "http://127.0.0.1:1",
                                 "model": "m", "api_key_env": TEST_KEY_ENV}).status_code == 200
        assert client.post("/api/capability-settings/text/test", json={}).status_code == 200

        # The image default has no credential variable, so naming an external provider there
        # makes the merged configuration invalid for the same reason PUT would reject it.
        assert client.put("/api/capability-settings/image", json={
            "provider": "demo", "endpoint": "", "model": "demo-svg-v1", "api_key_env": "",
        }).status_code == 200
        rejected = client.post("/api/capability-settings/image/test",
                               json={"provider": "openai", "endpoint": "http://127.0.0.1:1",
                                     "model": "m"})
        assert rejected.status_code == 422, rejected.text
        # The reused schema validator is what rejects it, so the reason names the missing variable.
        assert "api_key_env" in json.dumps(rejected.json(), ensure_ascii=False)
