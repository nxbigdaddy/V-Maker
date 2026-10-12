"""Image/speech provider adapters, capability settings, and shot-level media tasks.

Every adapter assertion runs against a real loopback HTTP server (``ThreadingHTTPServer``
on ``127.0.0.1:0``) rather than a monkeypatched stub, so the request path, the headers,
and the JSON body are observed exactly as the provider would send them.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.analysis import AnalysisRepository
from backend.main import create_app
from backend.providers import scrub_secret

# Minimal but genuinely valid 1x1 PNG and 44-byte WAV payloads.
IMAGE_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
)
AUDIO_BYTES = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00@\x1f\x00\x00"
    b"\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)
TEST_API_KEY = "sk-test-secret-vmaker-123"
TEST_KEY_ENV = "VMAKER_TEST_MEDIA_KEY"
IMAGE_JSON = {"data": [{"b64_json": base64.b64encode(IMAGE_BYTES).decode("ascii"), "output_format": "png"}]}


class MockProviderHandler(BaseHTTPRequestHandler):
    """Records every request and answers from the routing table the test installed."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - silence the test log
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._serve()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._serve()

    @property
    def provider(self) -> "MockProvider":
        return self.server.provider  # type: ignore[attr-defined]

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        # Record only the fields under test; the Authorization value is the credential itself.
        self.provider.record({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "body": raw,
        })
        should_hold = False
        with self.provider.condition:
            self.provider.received += 1
            if self.provider.hold_nth == self.provider.received:
                should_hold = True
                self.provider.hold_nth = None  # only the matching request is ever held
        if should_hold:
            self.provider.arrived.set()
            self.provider.release.wait(timeout=20)
        entry = self.provider.routes.get(self.path)
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


class MockProvider:
    """A loopback stand-in for an OpenAI-compatible image/speech service."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes, str]] = {}
        self.log: list[dict] = []
        self.received = 0
        self.hold_nth: int | None = None
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockProviderHandler)
        self.server.provider = self  # type: ignore[attr-defined]
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.release_blocked()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # --- routing table -------------------------------------------------

    def record(self, entry: dict) -> None:
        with self.condition:
            self.log.append(entry)
            self.condition.notify_all()

    def json_route(self, path: str, payload: dict, status: int = 200) -> None:
        self.routes[path] = (status, json.dumps(payload).encode("utf-8"), "application/json")

    def content_route(self, path: str, payload: bytes, content_type: str, status: int = 200) -> None:
        self.routes[path] = (status, payload, content_type)

    def error_route(self, path: str, status: int, message: str) -> None:
        self.json_route(path, {"error": {"message": message}}, status=status)

    def requests(self, path: str | None = None) -> list[dict]:
        entries = list(self.log)
        return [entry for entry in entries if entry["path"] == path] if path else entries

    def reset_requests(self) -> None:
        self.log.clear()

    # --- blocking support ----------------------------------------------

    def hold_request(self, nth: int) -> tuple[threading.Event, threading.Event]:
        """Hold the ``nth`` request (1-based, across all paths) until the returned release is set.

        Counting inside the handler makes the decision race-free: earlier requests always pass,
        so a test can let the first provider call finish and hold the next one.
        """
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.hold_nth = int(nth)
        return self.arrived, self.release

    def release_blocked(self) -> None:
        self.hold_nth = None
        self.release.set()


@pytest.fixture
def mock_provider():
    server = MockProvider()
    try:
        yield server
    finally:
        server.close()


# --- helpers ------------------------------------------------------------


def _project(client: TestClient, **overrides) -> dict:
    payload = {"name": "镜头媒体测试", "story": "女孩帮助机器人回家。", "target_duration_sec": 10}
    payload.update(overrides)
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _project_with_shots(client: TestClient, *, shot_count: int = 1, **overrides) -> tuple[dict, list[dict]]:
    project = _project(client, **overrides)
    task = client.post(f"/api/projects/{project['id']}/analysis-tasks").json()
    assert client.post(f"/api/tasks/{task['id']}/execute").status_code == 200
    assert client.post(f"/api/projects/{project['id']}/screenplay/generate").status_code == 201
    shots = client.get(f"/api/projects/{project['id']}/shots").json()
    assert len(shots) >= shot_count, shots
    return project, shots[:shot_count]


def _set_capability(client: TestClient, capability: str, **payload) -> dict:
    response = client.put(f"/api/capability-settings/{capability}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _external_image_capability(client: TestClient, endpoint: str, *, unit_cost: float = 0.0) -> dict:
    return _set_capability(client, "image", provider="openai", endpoint=endpoint, model="mock-image-v1",
                           api_key_env=TEST_KEY_ENV, unit_cost=unit_cost)


def _external_speech_capability(client: TestClient, endpoint: str) -> dict:
    return _set_capability(client, "speech", provider="openai", endpoint=endpoint, model="mock-speech-v1",
                           api_key_env=TEST_KEY_ENV, unit_cost=0.0)


def _execute(client: TestClient, task_id: str) -> dict:
    response = client.post(f"/api/tasks/{task_id}/execute")
    assert response.status_code == 200, response.text
    return response.json()


def _assets(client: TestClient, project_id: str, kind: str | None = None) -> list[dict]:
    assets = client.get(f"/api/projects/{project_id}/assets").json()
    return [asset for asset in assets if kind is None or asset["kind"] == kind]


def _session_assets(client: TestClient, project_id: str, kind: str, before: int) -> list[dict]:
    """Only the assets created after the recorded baseline, oldest first."""
    return sorted(_assets(client, project_id, kind), key=lambda item: item["created_at"])[before:]


def _body(entry: dict) -> dict:
    return json.loads(entry["body"].decode("utf-8"))


# --- 1. b64_json image --------------------------------------------------


def test_b64_image_is_registered_after_a_real_http_call(tmp_path: Path, monkeypatch, mock_provider) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_provider.json_route("/images/generations", IMAGE_JSON)
    with TestClient(create_app(tmp_path / "image_b64.db")) as client:
        project, shots = _project_with_shots(client, visual_style="赛博朋克夜景")
        _external_image_capability(client, mock_provider.url)
        created = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/image")
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["task_type"] == "shot_image" and body["capability"] == "image"
        assert body["provider"] == "openai" and body["model"] == "mock-image-v1"
        assert body["estimated_cost"] == 0
        assert body["events"][0]["event_type"] == "created"

        executed = _execute(client, body["id"])
        assert executed["status"] == "completed", executed["error"]

        posts = mock_provider.requests("/images/generations")
        assert len(posts) == 1, posts
        assert posts[0]["method"] == "POST"
        assert posts[0]["authorization"] == f"Bearer {TEST_API_KEY}"
        payload = _body(posts[0])
        # The final 16:9 frame is still the storyboard's 5s demo duration until media generation.
        assert payload["model"] == "mock-image-v1"
        assert payload["n"] == 1 and payload["size"] == "1280x720" and payload["response_format"] == "b64_json"
        assert "赛博朋克夜景" in payload["prompt"]
        assert shots[0]["action"] in payload["prompt"]
        assert shots[0]["shot_size"] in payload["prompt"]

        created_assets = _session_assets(client, project["id"], "image", 0)
        # Demo media never ran, so the only image asset is the one this task registered.
        assert len(created_assets) == 1
        asset = created_assets[0]
        assert asset["source_type"] == "import"
        assert asset["source_shot_id"] == shots[0]["id"]
        version = asset["latest_version"]
        assert version["demo"] is False
        assert version["mime_type"] == "image/png"
        assert version["byte_size"] == len(IMAGE_BYTES)
        assert version["metadata"]["provider"] == "openai"
        assert version["metadata"]["model"] == "mock-image-v1"
        assert version["metadata"]["prompt"] == payload["prompt"]
        assert version["metadata"]["shot_id"] == shots[0]["id"]
        assert version["metadata"]["demo"] is False

        content = client.get(f"/api/asset-versions/{version['id']}/content")
        assert content.status_code == 200, content.text
        assert content.content == IMAGE_BYTES
        assert content.headers["content-type"].startswith("image/png")


# --- 2. url image -------------------------------------------------------


def test_url_image_is_downloaded_with_the_same_authorization(tmp_path: Path, monkeypatch, mock_provider) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_provider.json_route("/images/generations", {"data": [{"url": "/file.png", "mime_type": "image/png"}]})
    mock_provider.content_route("/file.png", IMAGE_BYTES, "image/png")
    with TestClient(create_app(tmp_path / "image_url.db")) as client:
        project, shots = _project_with_shots(client, shot_count=2)
        _external_image_capability(client, mock_provider.url)
        # The batch route processes every shot of the project's latest version.
        task = client.post(f"/api/projects/{project['id']}/media-images").json()
        assert _execute(client, task["id"])["status"] == "completed"

        generations = mock_provider.requests("/images/generations")
        assert len(generations) == 2, generations
        downloads = mock_provider.requests("/file.png")
        assert len(downloads) == len(generations) == 2, downloads
        assert all(entry["authorization"] == f"Bearer {TEST_API_KEY}" for entry in generations + downloads)
        assert downloads[0]["method"] == "GET"

        assets = _assets(client, project["id"], "image")
        assert len(assets) == 2
        assert {asset["source_shot_id"] for asset in assets} == {shot["id"] for shot in shots}
        for asset in assets:
            version = asset["latest_version"]
            assert version["mime_type"] == "image/png"
            assert version["demo"] is False
            assert version["metadata"]["prompt"]  # every shot keeps its own assembled prompt
            assert client.get(f"/api/asset-versions/{version['id']}/content").content == IMAGE_BYTES


def test_url_image_mime_falls_back_to_magic_bytes(tmp_path: Path, monkeypatch, mock_provider) -> None:
    """A generic download Content-Type must not override the real image type."""
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_provider.json_route("/images/generations", {"data": [{"url": "/blob"}]})
    mock_provider.content_route("/blob", IMAGE_BYTES, "application/octet-stream")
    with TestClient(create_app(tmp_path / "image_magic.db")) as client:
        project, shots = _project_with_shots(client, shot_count=1)
        _external_image_capability(client, mock_provider.url)
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/image").json()
        assert _execute(client, task["id"])["status"] == "completed"
        version = _assets(client, project["id"], "image")[0]["latest_version"]
        assert version["mime_type"] == "image/png"  # probed from the PNG signature
        assert version["relative_path"].endswith(".png")


def test_url_image_download_failure_reports_the_status(tmp_path: Path, monkeypatch, mock_provider) -> None:
    """A download error keeps its own message instead of becoming a parse error."""
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_provider.json_route("/images/generations", {"data": [{"url": "/missing.png"}]})
    mock_provider.error_route("/missing.png", 404, f"no such file for {TEST_API_KEY}")
    with TestClient(create_app(tmp_path / "image_download_500.db")) as client:
        project, shots = _project_with_shots(client, shot_count=1)
        _external_image_capability(client, mock_provider.url)
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/image").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "failed"
        assert "HTTP 404" in executed["error"]
        assert "无法解析" not in executed["error"]
        assert TEST_API_KEY not in executed["error"]
        assert not _assets(client, project["id"], "image")


# --- 3. speech ----------------------------------------------------------


def test_speech_posts_audio_request_and_registers_wav(tmp_path: Path, monkeypatch, mock_provider) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    monkeypatch.delenv("VMAKER_SPEECH_VOICE", raising=False)
    mock_provider.content_route("/audio/speech", AUDIO_BYTES, "audio/wav")
    with TestClient(create_app(tmp_path / "speech.db")) as client:
        project, shots = _project_with_shots(client, shot_count=1)
        _external_speech_capability(client, mock_provider.url)
        created = client.post(f"/api/projects/{project['id']}/speech", json={"shot_ids": [shots[0]["id"]]})
        assert created.status_code == 201, created.text
        assert created.json()["task_type"] == "speech" and created.json()["capability"] == "speech"
        assert _execute(client, created.json()["id"])["status"] == "completed"

        posts = mock_provider.requests("/audio/speech")
        assert len(posts) == 1, posts
        assert posts[0]["authorization"] == f"Bearer {TEST_API_KEY}"
        payload = _body(posts[0])
        assert payload["model"] == "mock-speech-v1"
        assert payload["input"] == shots[0]["action"]
        # voice is omitted entirely when the caller has no voice configured.
        assert "voice" not in payload
        assert payload["response_format"] == "wav"

        assets = _assets(client, project["id"], "audio")
        assert len(assets) == 1
        version = assets[0]["latest_version"]
        assert version["mime_type"] == "audio/wav"
        assert version["demo"] is False
        assert version["metadata"]["voice"] == ""
        assert version["metadata"]["text"] == shots[0]["action"]
        content = client.get(f"/api/asset-versions/{version['id']}/content")
        assert content.content == AUDIO_BYTES


def test_speech_uses_configured_voice(tmp_path: Path, monkeypatch, mock_provider) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    monkeypatch.setenv("VMAKER_SPEECH_VOICE", "alloy")
    mock_provider.content_route("/audio/speech", AUDIO_BYTES, "audio/wav")
    with TestClient(create_app(tmp_path / "speech_voice.db")) as client:
        project, shots = _project_with_shots(client, shot_count=1)
        _external_speech_capability(client, mock_provider.url)
        task = client.post(f"/api/projects/{project['id']}/speech", json={"shot_ids": [shots[0]["id"]]}).json()
        assert _execute(client, task["id"])["status"] == "completed"
        posts = mock_provider.requests("/audio/speech")
        assert len(posts) == 1
        assert _body(posts[0])["voice"] == "alloy"


# --- 4. provider HTTP failure ------------------------------------------


def test_provider_http_error_fails_the_task_without_assets(tmp_path: Path, monkeypatch, mock_provider) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    # The upstream error body echoes the credential: it must never reach the task record.
    mock_provider.error_route("/images/generations", 500, f"upstream exploded for key {TEST_API_KEY}")
    with TestClient(create_app(tmp_path / "image_500.db")) as client:
        project, shots = _project_with_shots(client)
        _external_image_capability(client, mock_provider.url)
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/image").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "failed"
        assert "HTTP 500" in executed["error"]
        # The key must not leak into the task, and the upstream body is never echoed back at all.
        assert TEST_API_KEY not in executed["error"]
        assert "upstream exploded" not in executed["error"]
        assert not _assets(client, project["id"], "image")
        events = {event["event_type"]: event["message"] for event in executed["events"]}
        assert TEST_API_KEY not in json.dumps(events, ensure_ascii=False)
        assert client.get(f"/api/projects/{project['id']}/timeline").status_code == 404


# --- 5. missing environment variable -----------------------------------


def test_missing_api_key_environment_variable_fails_honestly(tmp_path: Path, monkeypatch, mock_provider) -> None:
    monkeypatch.delenv(TEST_KEY_ENV, raising=False)
    mock_provider.json_route("/images/generations", IMAGE_JSON)
    with TestClient(create_app(tmp_path / "image_nokey.db")) as client:
        project, shots = _project_with_shots(client)
        configured = _external_image_capability(client, mock_provider.url)
        assert configured["configured"] is False
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/image").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "failed"
        assert TEST_KEY_ENV in executed["error"]
        assert not mock_provider.requests()
        assert not _assets(client, project["id"], "image")


# --- 6. cancellation ----------------------------------------------------


def test_canceled_shot_task_stops_before_the_next_shot(tmp_path: Path, monkeypatch, mock_provider) -> None:
    """The first shot completes, then the run is canceled while the second call is in flight."""
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_provider.json_route("/images/generations", IMAGE_JSON)
    arrived, release = mock_provider.hold_request(2)
    with TestClient(create_app(tmp_path / "image_cancel.db")) as client:
        project, shots = _project_with_shots(client, shot_count=2)
        _external_image_capability(client, mock_provider.url)
        # Queue the very task the endpoint would queue, but execute it explicitly: the endpoint's
        # BackgroundTasks run inside the POST, which would leave no window for a cancel.
        repo = AnalysisRepository(client.app.state.database)
        task, reason = repo.create_shot_media_task(project["id"], "shot_image")
        assert task is not None and reason is None, reason
        task_id = str(task["id"])
        assert task["estimated_cost"] == 0

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(_execute, client, task_id)
            # The first provider call finishes; the second is held open so the cancel lands mid-run.
            assert arrived.wait(timeout=15), "the second shot was never held by the mock"
            assert len(_assets(client, project["id"], "image")) == 1
            canceled = client.post(f"/api/tasks/{task_id}/cancel").json()
            assert canceled["status"] == "canceled"
            release.set()
            executed = running.result(timeout=30)

        # The run re-checked the task status and stopped instead of completing the batch.
        assert executed["status"] == "canceled"
        images = _assets(client, project["id"], "image")
        # Only the first shot was produced, and no partial asset was left behind.
        assert len(images) == 1, images
        assert images[0]["source_shot_id"] == shots[0]["id"]
        assert images[0]["latest_version"]["demo"] is False


# --- 7. budget ----------------------------------------------------------


def test_shot_images_are_rejected_when_the_budget_is_too_small(tmp_path: Path, monkeypatch, mock_provider) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_provider.json_route("/images/generations", IMAGE_JSON)
    with TestClient(create_app(tmp_path / "image_budget.db")) as client:
        project, shots = _project_with_shots(client, shot_count=2, budget_limit=1.0)
        _external_image_capability(client, mock_provider.url, unit_cost=0.6)
        created = client.post(f"/api/projects/{project['id']}/media-images")
        assert created.status_code == 201, created.text
        assert created.json()["estimated_cost"] == pytest.approx(1.2)
        executed = _execute(client, created.json()["id"])
        assert executed["status"] == "failed"
        assert "budget limit exceeded" in executed["error"]
        assert any(event["event_type"] == "budget_rejected" for event in executed["events"])
        assert not _assets(client, project["id"], "image")
        assert not mock_provider.requests()
        costs = client.get(f"/api/projects/{project['id']}/costs").json()
        assert costs["budget"]["reserved"] == 0.0 and costs["budget"]["settled"] == 0.0


def test_single_shot_image_task_estimates_one_shot(tmp_path: Path, monkeypatch, mock_provider) -> None:
    """The per-shot route reserves exactly one shot's cost, not the whole project's."""
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_provider.json_route("/images/generations", IMAGE_JSON)
    with TestClient(create_app(tmp_path / "image_cost.db")) as client:
        project, shots = _project_with_shots(client, shot_count=2)
        _external_image_capability(client, mock_provider.url, unit_cost=0.25)
        created = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/image")
        assert created.status_code == 201, created.text
        assert created.json()["estimated_cost"] == pytest.approx(0.25)
        assert _execute(client, created.json()["id"])["status"] == "completed"
        assert len(mock_provider.requests("/images/generations")) == 1

        batch = client.post(f"/api/projects/{project['id']}/media-images")
        assert batch.status_code == 201
        assert batch.json()["estimated_cost"] == pytest.approx(0.5)  # two shots at 0.25 each


# --- request validation -------------------------------------------------


def test_shot_media_task_request_validation(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "image_validation.db")) as client:
        project, shots = _project_with_shots(client, shot_count=1)
        other, other_shots = _project_with_shots(client, shot_count=1)

        assert client.post("/api/projects/missing/media-images", json={}).status_code == 404
        assert client.post("/api/projects/missing/speech", json={}).status_code == 404
        assert client.post(f"/api/projects/{project['id']}/shots/shot_missing/image").status_code == 422
        # A shot that exists but belongs to another project must not be reported as a 404 success.
        foreign = client.post(f"/api/projects/{project['id']}/shots/{other_shots[0]['id']}/image")
        assert foreign.status_code == 422
        assert client.post(f"/api/projects/{project['id']}/media-images", json={"shot_ids": []}).status_code == 422
        assert client.post(f"/api/projects/{project['id']}/media-images",
                           json={"shot_ids": ["shot_missing"]}).status_code == 422
        assert client.post(f"/api/projects/{project['id']}/media-images",
                           json={"shot_ids": [shots[0]["id"], shots[0]["id"]]}).status_code == 422
        assert client.post(f"/api/projects/{project['id']}/speech", json={"shot_ids": "nope"}).status_code == 422

        created = client.post(f"/api/projects/{project['id']}/media-images")
        assert created.status_code == 201
        assert created.json()["task_type"] == "shot_image" and created.json()["provider"] == "demo"
        # The queued task is visible on the project task list exactly like every other task.
        listed = client.get(f"/api/projects/{project['id']}/tasks").json()
        assert created.json()["id"] in [item["id"] for item in listed]


def test_demo_shot_image_is_marked_as_a_local_placeholder(tmp_path: Path) -> None:
    """The default image capability is the honest demo renderer, not a hidden cloud call."""
    with TestClient(create_app(tmp_path / "image_demo.db")) as client:
        project, shots = _project_with_shots(client)
        created = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/image")
        assert created.status_code == 201
        assert created.json()["provider"] == "demo" and created.json()["estimated_cost"] == 0
        assert _execute(client, created.json()["id"])["status"] == "completed"
        version = _assets(client, project["id"], "image")[0]["latest_version"]
        assert version["demo"] is True
        # 位图而不是 SVG：FFmpeg 与剪映都读不了 SVG，demo 又是默认素材。
        assert version["mime_type"] == "image/png"
        assert "不是 AI 生成" in version["metadata"]["notice"]
        body = client.get(f"/api/asset-versions/{version['id']}/content").content
        assert body[:8] == b"\x89PNG\r\n\x1a\n"


# --- 8. capability settings API ----------------------------------------


def test_capability_settings_api_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(TEST_KEY_ENV, raising=False)
    with TestClient(create_app(tmp_path / "capability.db")) as client:
        listed = client.get("/api/capability-settings")
        assert listed.status_code == 200
        assert [row["capability"] for row in listed.json()] == ["text", "image", "speech", "video"]
        # Fresh databases default the media capabilities to the local demo generators.
        defaults = {row["capability"]: row for row in listed.json()}
        assert defaults["image"]["provider"] == "demo" and defaults["image"]["model"] == "demo-bitmap-v1"
        assert defaults["speech"]["provider"] == "demo" and defaults["speech"]["model"] == "demo-tone-v1"
        assert defaults["image"]["configured"] is True and defaults["speech"]["configured"] is True
        # 视频没有诚实的本地占位实现，默认就是「不支持」，而不是给一个假视频。
        # 视频的 demo 会用本地 FFmpeg 渲染真实占位片段；没有 FFmpeg 时才如实失败。
        assert defaults["video"]["provider"] == "demo" and defaults["video"]["model"] == "demo-ffmpeg-v1"

        updated = client.put("/api/capability-settings/image", json={
            "provider": "dashscope", "endpoint": "https://dashscope.aliyuncs.com/api/v1",
            "model": "wanx-v1", "api_key_env": TEST_KEY_ENV, "unit_cost": 0.6,
        })
        assert updated.status_code == 200, updated.text
        assert updated.json()["unit_cost"] == pytest.approx(0.6)
        assert updated.json()["configured"] is False
        fetched = client.get("/api/capability-settings/image")
        assert fetched.json() == updated.json()

        monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
        assert client.get("/api/capability-settings/image").json()["configured"] is True
        assert client.get("/api/capability-settings").json()[1]["configured"] is True
        monkeypatch.delenv(TEST_KEY_ENV, raising=False)
        assert client.get("/api/capability-settings/image").json()["configured"] is False

        assert client.get("/api/capability-settings/text").status_code == 200
        assert client.get("/api/capability-settings/audio").status_code == 404
        assert client.put("/api/capability-settings/audio", json={
            "provider": "demo", "model": "x",
        }).status_code == 404
        # provider/model must not be blank; api_key_env must look like a variable name.
        assert client.put("/api/capability-settings/image", json={
            "provider": "demo", "model": "m", "api_key_env": "lower-case",
        }).status_code == 422
        assert client.put("/api/capability-settings/image", json={"provider": "  ", "model": "m"}).status_code == 422
        assert client.put("/api/capability-settings/image", json={"provider": "demo", "model": ""}).status_code == 422
        assert client.put("/api/capability-settings/image", json={
            "provider": "demo", "model": "m", "unit_cost": -1,
        }).status_code == 422
        # A demo capability needs no credential, so an empty variable name is accepted there.
        assert client.put("/api/capability-settings/image", json={
            "provider": "demo", "endpoint": "", "model": "demo-svg-v1", "api_key_env": "", "unit_cost": 0,
        }).status_code == 200


def test_text_capability_and_provider_settings_are_one_record(tmp_path: Path) -> None:
    """``/api/provider-settings`` and ``/api/capability-settings/text`` must read the same row."""
    def shared(capability_row: dict) -> dict:
        return {key: value for key, value in capability_row.items() if key not in ("capability", "unit_cost")}

    with TestClient(create_app(tmp_path / "text_shared.db")) as client:
        before = client.get("/api/provider-settings").json()
        assert client.get("/api/capability-settings/text").json() == {
            "capability": "text", "unit_cost": 0.0, **before,
        }
        assert shared(client.get("/api/capability-settings/text").json()) == before

        assert client.put("/api/provider-settings", json={
            "provider": "deepseek", "endpoint": "https://api.deepseek.com/v1",
            "model": "deepseek-chat", "api_key_env": "DEEPSEEK_API_KEY",
        }).status_code == 200
        after = client.get("/api/provider-settings").json()
        assert after["provider"] == "deepseek" and after["model"] == "deepseek-chat"
        assert shared(client.get("/api/capability-settings/text").json()) == after
        assert client.get("/api/capability-settings/text").json()["capability"] == "text"

        # Writing through the capability route is visible through the legacy route as well.
        assert client.put("/api/capability-settings/text", json={
            "provider": "demo", "endpoint": "https://api.openai.com/v1",
            "model": "deterministic-v1", "api_key_env": "OPENAI_API_KEY",
        }).status_code == 200
        assert client.get("/api/provider-settings").json()["provider"] == "demo"
        assert client.get("/api/settings/text-provider").json()["provider"] == "demo"
        assert shared(client.get("/api/capability-settings/text").json()) == client.get("/api/provider-settings").json()
        # Rewriting the text provider must not disturb the media capabilities.
        assert client.get("/api/capability-settings/image").json()["model"] == "demo-bitmap-v1"


# --- scrubbing helper ---------------------------------------------------


def test_legacy_demo_image_model_name_is_renamed_on_startup(tmp_path: Path) -> None:
    """demo 占位图从 SVG 改成位图后，旧库里那个已种下的名字要跟着改，否则界面在说谎。"""
    path = tmp_path / "legacy_demo.db"
    with TestClient(create_app(path)) as client:
        with client.app.state.database.connect() as connection:
            connection.execute(
                "UPDATE capability_settings SET model='demo-svg-v1' WHERE capability='image'")
    # 重启后端＝重新初始化数据库。
    with TestClient(create_app(path)) as client:
        rows = {row["capability"]: row for row in client.get("/api/capability-settings").json()}
        assert rows["image"]["model"] == "demo-bitmap-v1", rows["image"]


def test_renaming_the_demo_model_never_touches_a_user_edited_row(tmp_path: Path) -> None:
    path = tmp_path / "user_edited.db"
    with TestClient(create_app(path)) as client:
        with client.app.state.database.connect() as connection:
            connection.execute(
                "UPDATE capability_settings SET provider='openai', model='gpt-image-1'"
                " WHERE capability='image'")
    with TestClient(create_app(path)) as client:
        rows = {row["capability"]: row for row in client.get("/api/capability-settings").json()}
        assert rows["image"]["provider"] == "openai"
        assert rows["image"]["model"] == "gpt-image-1", rows["image"]


def test_shot_image_prompt_assembles_shot_scene_and_style() -> None:
    """The prompt builder is a standalone function so it can be asserted without any task."""
    from backend.analysis import shot_image_prompt, shot_speech_text

    prompt = shot_image_prompt(
        {"visual_style": "水墨动画", "creative_intent": "温柔"},
        {"title": "雨夜天台", "setting": "城市屋顶", "purpose": "相遇"},
        {"action": "女孩奔跑", "shot_size": "wide", "camera": "low_angle",
         "narration_dialogue": "我一定会回来。"},
    )
    for fragment in ("水墨动画", "雨夜天台", "城市屋顶", "wide", "low_angle", "女孩奔跑", "我一定会回来。"):
        assert fragment in prompt, fragment
    # Split storyboard camera fields take precedence over the stored single camera string.
    split = shot_image_prompt({}, {}, {"action": "走", "camera": "static", "camera_angle": "俯拍",
                                       "camera_motion": "推轨"})
    assert "俯拍" in split and "推轨" in split and "static" not in split
    # Missing fields fall back to explicit Chinese placeholders rather than empty text.
    fallback = shot_image_prompt(None, None, None)
    assert "未指定视觉风格" in fallback and "medium" in fallback

    assert shot_speech_text({"narration_dialogue": " 台词 ", "action": "动作"}) == "台词"
    assert shot_speech_text({"narration_dialogue": "  ", "action": "动作"}) == "动作"
    assert shot_speech_text({}) == ""


def test_scrub_secret_removes_every_echo_of_the_key() -> None:
    key = "sk-abc123456789"
    assert key not in scrub_secret(f"failed with {key} twice {key}", key)
    assert "[REDACTED]" in scrub_secret(f"failed with {key}", key)
    assert key not in scrub_secret(f"Bearer {key} rejected", key)
    assert scrub_secret("no secret here", "") == "no secret here"
    assert scrub_secret("plain failure", None) == "plain failure"
