"""Image-to-video provider: the honest demo refusal, the submit/poll adapter, and shot tasks.

Every adapter assertion runs against a real loopback HTTP server (``ThreadingHTTPServer`` on
``127.0.0.1:0``) rather than a monkeypatched stub, so the request path, the headers, and the
JSON body are observed exactly as the provider would send them.

The convention exercised here is V-Maker's own, not any vendor's standard API; see
``OpenAICompatibleVideoProvider`` for why that distinction is spelled out in the code.
"""
from __future__ import annotations

import base64
import concurrent.futures
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.analysis import AnalysisRepository
from backend.config import Settings
from backend.database import Database
from backend.main import create_app
from backend.media import MediaRepository

# A genuinely valid 1x1 PNG, and a byte string shaped like a real MP4 (``ftyp`` box in place).
IMAGE_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
)
VIDEO_BYTES = (bytes.fromhex("00000018") + b"ftyp" + b"isom"
               + b"v-maker-mock-video-payload" * 4)
TEST_API_KEY = "sk-test-secret-vmaker-video-789"
TEST_KEY_ENV = "VMAKER_TEST_VIDEO_KEY"
SUBMIT_PATH = "/video/generations"
POLL_PATH = "/video/generations/job-1"
DOWNLOAD_PATH = "/files/shot.mp4"


class MockVideoHandler(BaseHTTPRequestHandler):
    """Records every request and answers from the dynamic routing table the test installed."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - silence the test log
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._serve()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._serve()

    @property
    def service(self) -> "MockVideoService":
        return self.server.service  # type: ignore[attr-defined]

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0]
        # Record only the fields under test; the Authorization value is the credential itself.
        entry = {
            "method": self.command,
            "path": path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "body": raw,
        }
        self.service.record(entry)
        # A route may be a fixed answer or a function of the request count, and may itself have a
        # side effect (the cancellation test cancels the task from inside one poll).
        service = self.service
        responder = service.handlers.get(path)
        if responder is None:
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        count = service.count(path)
        if service.callback is not None:
            service.callback(path, count, self.headers)
        status, payload, content_type = responder(count)
        self._send(status, payload, content_type)

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MockVideoService:
    """A loopback stand-in for an asynchronous image-to-video service."""

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.log: list[dict] = []
        self.callback = None
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockVideoHandler)
        self.server.service = self  # type: ignore[attr-defined]
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

    # --- routing table -------------------------------------------------

    def record(self, entry: dict) -> None:
        with self.lock:
            self.log.append(entry)

    def count(self, path: str) -> int:
        """How many requests this path has received, including the one being served."""
        with self.lock:
            return len([item for item in self.log if item["path"] == path])

    def json_route(self, path: str, payload: dict, status: int = 200) -> None:
        blob = json.dumps(payload).encode("utf-8")
        self.handlers[path] = lambda _count: (status, blob, "application/json")

    def payload_route(self, path: str, payload: bytes, content_type: str, status: int = 200) -> None:
        self.handlers[path] = lambda _count: (status, payload, content_type)

    def status_route(self, path: str, states: list[dict]) -> None:
        """Answer with one status document per poll, repeating the last one forever."""
        def responder(count: int):
            state = states[min(max(count, 1), len(states)) - 1]
            return 200, json.dumps(state).encode("utf-8"), "application/json"
        self.handlers[path] = responder

    def error_route(self, path: str, status: int, message: str) -> None:
        self.json_route(path, {"error": {"message": message}}, status=status)

    def requests(self, path: str | None = None) -> list[dict]:
        with self.lock:
            entries = list(self.log)
        return [entry for entry in entries if entry["path"] == path] if path else entries

    def paths(self) -> list[str]:
        return [entry["path"] for entry in self.requests()]


@pytest.fixture
def mock_video():
    service = MockVideoService()
    try:
        yield service
    finally:
        service.close()


@pytest.fixture
def fast_video_runtime(monkeypatch):
    """Keep submit-then-poll tests quick without changing the production defaults."""
    monkeypatch.setenv("VMAKER_VIDEO_POLL_SECONDS", "0.01")
    monkeypatch.setenv("VMAKER_VIDEO_TIMEOUT_SECONDS", "20")


# --- helpers ------------------------------------------------------------


def _project(client: TestClient, **overrides) -> dict:
    payload = {"name": "镜头视频测试", "story": "女孩帮助机器人回家。", "target_duration_sec": 10}
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


def _project_with_images(client: TestClient, *, shot_count: int = 1, **overrides) -> tuple[dict, list[dict]]:
    """A project whose shots each have a real demo still frame, ready to be animated."""
    project, shots = _project_with_shots(client, shot_count=shot_count, **overrides)
    before = len(_assets(client, project["id"], "image"))
    created = client.post(f"/api/projects/{project['id']}/media-images")
    assert created.status_code == 201, created.text
    assert client.post(f"/api/tasks/{created.json()['id']}/execute").status_code == 200
    after = len(_assets(client, project["id"], "image"))
    assert after > before
    return project, shots


def _set_capability(client: TestClient, capability: str, **payload) -> dict:
    response = client.put(f"/api/capability-settings/{capability}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _external_video_capability(client: TestClient, endpoint: str, *, unit_cost: float = 0.0,
                               model: str = "mock-video-v1") -> dict:
    return _set_capability(client, "video", provider="openai", endpoint=endpoint, model=model,
                           api_key_env=TEST_KEY_ENV, unit_cost=unit_cost)


def _execute(client: TestClient, task_id: str) -> dict:
    response = client.post(f"/api/tasks/{task_id}/execute")
    assert response.status_code == 200, response.text
    return response.json()


def _assets(client: TestClient, project_id: str, kind: str | None = None) -> list[dict]:
    assets = client.get(f"/api/projects/{project_id}/assets").json()
    return [asset for asset in assets if kind is None or asset["kind"] == kind]


def _body(entry: dict) -> dict:
    return json.loads(entry["body"].decode("utf-8"))


def _events(task: dict) -> list[str]:
    return [str(event["message"]) for event in task["events"]]


def _cancel_task(database: Database, task_id: str) -> None:
    """Cancel from outside the executor, exactly as ``POST /api/tasks/{id}/cancel`` would."""
    AnalysisRepository(database).cancel(task_id)


# --- 1. demo placeholder clip -------------------------------------------


def test_demo_video_without_ffmpeg_fails_honestly_and_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """没有 FFmpeg 就没有诚实的产物：必须失败并说清缺什么，绝不能伪造一个假视频。"""
    monkeypatch.delenv(TEST_KEY_ENV, raising=False)
    monkeypatch.setattr(MediaRepository, "_ffmpeg", staticmethod(lambda: None))
    with TestClient(create_app(tmp_path / "video_demo_noffmpeg.db")) as client:
        project, shots = _project_with_images(client, shot_count=1)
        capability = client.get("/api/capability-settings/video").json()
        assert capability["provider"] == "demo" and capability["model"] == "demo-ffmpeg-v1"

        created = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video")
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["task_type"] == "shot_video" and body["capability"] == "video"
        assert body["provider"] == "demo" and body["estimated_cost"] == 0

        executed = _execute(client, body["id"])
        assert executed["status"] == "failed"
        assert "FFmpeg" in executed["error"]
        assert "imageio-ffmpeg" in executed["error"]
        # 没有 ffmpeg 就什么都没产出。
        assert _assets(client, project["id"], "video") == []
        assert _assets(client, project["id"], "image")


def test_demo_video_with_ffmpeg_produces_a_real_decodable_clip(tmp_path: Path, monkeypatch) -> None:
    """有 FFmpeg 时，demo 会渲染一个**真实可解码**的占位片段，而不是继续拒绝。

    这正是「占位素材不可能成为真实视频」不再成立的地方：本地有了编码器，占位画面就能变成
    一个真片段，让视频链路（时间线、MP4 导出、剪映草稿）在不为服务付费的前提下可被验证。
    """
    monkeypatch.delenv(TEST_KEY_ENV, raising=False)
    ffmpeg = MediaRepository._ffmpeg()
    if not ffmpeg:
        pytest.skip("no ffmpeg available to render a placeholder clip")
    with TestClient(create_app(tmp_path / "video_demo_ffmpeg.db")) as client:
        project, shots = _project_with_images(client, shot_count=1)
        created = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video")
        assert created.status_code == 201, created.text
        executed = _execute(client, created.json()["id"])
        assert executed["status"] == "completed", executed

        videos = _assets(client, project["id"], "video")
        assert len(videos) == 1
        version = videos[0]["latest_version"]
        assert version["mime_type"] == "video/mp4"
        assert version["demo"] is True
        assert version["relative_path"].endswith(".mp4")
        assert "占位" in version["metadata"]["notice"] or "placeholder" in version["metadata"]["notice"].lower()

        body = client.get(f"/api/asset-versions/{version['id']}/content").content
        assert body[4:8] == b"ftyp", body[:12]
        probe = subprocess.run([ffmpeg, "-v", "error", "-i", "-", "-f", "null", "-"],
                               input=body, capture_output=True, timeout=180)
        # 完整解码一遍：只有真的可解码才算产出，而不是"写了个文件"。
        assert probe.returncode == 0, probe.stderr.decode("utf-8", "replace")[:300]


# --- 2. the honest asynchronous path ------------------------------------


def test_submit_poll_and_fetch_registers_a_real_video(
    tmp_path: Path, monkeypatch, mock_video, fast_video_runtime
) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_video.json_route(SUBMIT_PATH, {"task_id": "job-1"})
    mock_video.status_route(POLL_PATH, [
        {"status": "queued", "progress": 0.0, "message": "排队中"},
        {"status": "processing", "progress": 0.45, "message": "正在生成"},
        {"status": "succeeded", "progress": 1.0, "video_url": DOWNLOAD_PATH},
    ])
    mock_video.payload_route(DOWNLOAD_PATH, VIDEO_BYTES, "video/mp4")
    with TestClient(create_app(tmp_path / "video_ok.db")) as client:
        project, shots = _project_with_images(client, shot_count=1, visual_style="赛博朋克夜景")
        project_shots = client.get(f"/api/projects/{project['id']}/shots").json()
        images = _assets(client, project["id"], "image")
        _external_video_capability(client, mock_video.url)

        created = client.post(f"/api/projects/{project['id']}/media-videos")
        assert created.status_code == 201, created.text
        assert created.json()["capability"] == "video"
        assert _execute(client, created.json()["id"])["status"] == "completed"

        # The batch route animates every shot of the project, one submit each.
        submits = mock_video.requests(SUBMIT_PATH)
        assert len(submits) == len(project_shots), mock_video.paths()
        assert all(entry["method"] == "POST" for entry in submits)
        assert all(entry["authorization"] == f"Bearer {TEST_API_KEY}" for entry in submits)
        payloads = [_body(entry) for entry in submits]
        assert all(payload["model"] == "mock-video-v1" for payload in payloads)
        # demo 占位首帧现在是位图；视频 provider 收到的 MIME 必须跟素材一致。
        assert all(payload["image_mime_type"] == "image/png" for payload in payloads)
        assert all(payload["duration_sec"] == shots[0]["duration"] for payload in payloads)
        assert all(payload["aspect_ratio"] == "16:9" for payload in payloads)
        assert all("赛博朋克夜景" in payload["prompt"] for payload in payloads)
        # Each shot's own frame and its own motion brief, in shot order.
        for payload, shot in zip(payloads, project_shots):
            image_asset = next(asset for asset in images if asset["source_shot_id"] == shot["id"])
            stored = client.get(f"/api/asset-versions/{image_asset['latest_version']['id']}/content")
            assert base64.b64decode(payload["image_base64"]) == stored.content
            assert shot["action"] in payload["prompt"]
            assert payload["camera"] == shot["camera"]

        polls = mock_video.requests(POLL_PATH)
        assert [entry["method"] for entry in polls] == ["GET"] * len(polls)
        assert len(polls) >= 2, mock_video.paths()
        assert all(entry["authorization"] == f"Bearer {TEST_API_KEY}" for entry in polls)

        downloads = mock_video.requests(DOWNLOAD_PATH)
        assert len(downloads) == len(project_shots), mock_video.paths()
        assert all(entry["method"] == "GET" for entry in downloads)
        assert all(entry["authorization"] == f"Bearer {TEST_API_KEY}" for entry in downloads)

        videos = _assets(client, project["id"], "video")
        assert len(videos) == len(project_shots), videos
        assert {asset["source_shot_id"] for asset in videos} == {shot["id"] for shot in project_shots}
        assert all(asset["source_type"] == "import" for asset in videos)
        for asset in videos:
            version = asset["latest_version"]
            assert version["demo"] is False
            assert version["mime_type"] == "video/mp4"
            assert version["relative_path"].endswith(".mp4")
            assert version["byte_size"] == len(VIDEO_BYTES)
            metadata = version["metadata"]
            assert metadata["provider"] == "openai" and metadata["model"] == "mock-video-v1"
            assert metadata["duration_sec"] == shots[0]["duration"]
            assert metadata["notice"]
            # The metadata names the exact still frame this clip was animated from.
            image_asset = next(item for item in images if item["source_shot_id"] == asset["source_shot_id"])
            assert metadata["source_image_asset_id"] == image_asset["id"]
            content = client.get(f"/api/asset-versions/{version['id']}/content")
            assert content.status_code == 200, content.text
            assert content.content == VIDEO_BYTES
            assert content.headers["content-type"].startswith("video/mp4")


def test_video_mime_falls_back_to_magic_bytes(tmp_path: Path, monkeypatch, mock_video,
                                              fast_video_runtime) -> None:
    """A generic download Content-Type must not override the real container type."""
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    mock_video.status_route(POLL_PATH, [{"status": "succeeded", "video_url": DOWNLOAD_PATH}])
    mock_video.payload_route(DOWNLOAD_PATH, VIDEO_BYTES, "application/octet-stream")
    with TestClient(create_app(tmp_path / "video_magic.db")) as client:
        project, shots = _project_with_images(client, shot_count=1)
        _external_video_capability(client, mock_video.url)
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video").json()
        assert _execute(client, task["id"])["status"] == "completed"
        version = _assets(client, project["id"], "video")[0]["latest_version"]
        assert version["mime_type"] == "video/mp4"
        assert version["relative_path"].endswith(".mp4")


# --- 3. progress is visible ---------------------------------------------


def test_poll_progress_is_reported_in_the_task_message(tmp_path: Path, monkeypatch, mock_video,
                                                       fast_video_runtime) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    mock_video.status_route(POLL_PATH, [
        {"status": "processing", "progress": 0.45, "message": "正在生成"},
        {"status": "processing", "progress": 0.9, "message": "正在生成"},
        {"status": "succeeded", "video_url": DOWNLOAD_PATH},
    ])
    mock_video.payload_route(DOWNLOAD_PATH, VIDEO_BYTES, "video/mp4")
    with TestClient(create_app(tmp_path / "video_progress.db")) as client:
        project, shots = _project_with_images(client, shot_count=2)
        _external_video_capability(client, mock_video.url)
        task = client.post(f"/api/projects/{project['id']}/media-videos").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "completed", executed["error"]
        messages = " | ".join(_events(executed))
        # A poll's progress reaches the task as an observable update, with the shot position.
        assert "第 1/2 个镜头" in messages and "第 2/2 个镜头" in messages
        assert "生成中" in messages
        assert "45%" in messages or "90%" in messages
        assert len(mock_video.requests(POLL_PATH)) >= 2


# --- 4. a shot with no still frame submits nothing ----------------------


def test_shot_without_an_image_fails_before_any_submit(tmp_path: Path, monkeypatch,
                                                       mock_video) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    with TestClient(create_app(tmp_path / "video_noimage.db")) as client:
        project, shots = _project_with_shots(client, shot_count=1)
        assert _assets(client, project["id"], "image") == []
        _external_video_capability(client, mock_video.url)
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "failed"
        assert "还没有画面" in executed["error"]
        assert "请先生成画面" in executed["error"]
        # Nothing was ever sent to the provider: there was no frame to animate.
        assert mock_video.requests() == []
        assert _assets(client, project["id"], "video") == []


# --- 5. the provider reports failure ------------------------------------


def test_provider_reported_failure_leaves_no_asset(tmp_path: Path, monkeypatch, mock_video,
                                                   fast_video_runtime) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    mock_video.status_route(POLL_PATH, [
        {"status": "processing", "progress": 0.2},
        {"status": "failed", "error": "内容审核未通过：画面包含违规元素"},
    ])
    with TestClient(create_app(tmp_path / "video_failed.db")) as client:
        project, shots = _project_with_images(client, shot_count=1)
        _external_video_capability(client, mock_video.url)
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "failed"
        assert "视频生成失败" in executed["error"]
        assert "内容审核未通过" in executed["error"]
        assert _assets(client, project["id"], "video") == []
        # The download route was never even requested.
        assert mock_video.requests(DOWNLOAD_PATH) == []


def test_unknown_poll_status_is_not_reported_as_success(tmp_path: Path, monkeypatch, mock_video,
                                                        fast_video_runtime) -> None:
    """An unrecognised status must keep the run alive and say so, never be read as success."""
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    monkeypatch.setenv("VMAKER_VIDEO_TIMEOUT_SECONDS", "0.6")
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    mock_video.status_route(POLL_PATH, [{"status": "waiting_human_review", "progress": 0.5}])
    with TestClient(create_app(tmp_path / "video_unknown.db")) as client:
        project, shots = _project_with_images(client, shot_count=1)
        _external_video_capability(client, mock_video.url)
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "failed"
        assert "超时" in executed["error"]
        messages = " | ".join(_events(executed))
        assert "waiting_human_review" in messages
        assert _assets(client, project["id"], "video") == []


# --- 6. timeout ---------------------------------------------------------


def test_polling_timeout_fails_without_an_asset(tmp_path: Path, monkeypatch, mock_video) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    monkeypatch.setenv("VMAKER_VIDEO_POLL_SECONDS", "0.01")
    # A sub-second total budget: the first poll already lands past the deadline.
    monkeypatch.setenv("VMAKER_VIDEO_TIMEOUT_SECONDS", "0.05")
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    mock_video.status_route(POLL_PATH, [{"status": "processing", "progress": 0.3, "message": "还在处理"}])
    with TestClient(create_app(tmp_path / "video_timeout.db")) as client:
        project, shots = _project_with_images(client, shot_count=1)
        _external_video_capability(client, mock_video.url)
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "failed"
        assert "超时" in executed["error"]
        assert "未完成" in executed["error"]
        assert len(mock_video.requests(POLL_PATH)) >= 1
        assert mock_video.requests(DOWNLOAD_PATH) == []
        assert _assets(client, project["id"], "video") == []


# --- 7. cancellation during polling -------------------------------------


def test_cancel_during_polling_stops_the_run_without_an_asset(tmp_path: Path, monkeypatch,
                                                              mock_video) -> None:
    """The task is canceled from inside the second poll, so the stop is deterministic."""
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    monkeypatch.setenv("VMAKER_VIDEO_POLL_SECONDS", "0.01")
    monkeypatch.setenv("VMAKER_VIDEO_TIMEOUT_SECONDS", "30")
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    mock_video.status_route(POLL_PATH, [{"status": "processing", "progress": 0.4, "message": "生成中"}])
    with TestClient(create_app(tmp_path / "video_cancel.db")) as client:
        project, shots = _project_with_images(client, shot_count=1)
        _external_video_capability(client, mock_video.url)
        repo = AnalysisRepository(client.app.state.database)
        # Queue exactly what the route queues, but execute it explicitly so the cancel can land
        # mid-run instead of inside the endpoint's BackgroundTasks.
        task, reason = repo.create_shot_media_task(project["id"], "shot_video", [shots[0]["id"]])
        assert task is not None and reason is None, reason
        task_id = str(task["id"])
        assert task["capability"] == "video"

        def cancel_on_second_poll(path: str, count: int, _headers) -> None:
            if path == POLL_PATH and count == 2:
                _cancel_task(client.app.state.database, task_id)

        mock_video.callback = cancel_on_second_poll
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(_execute, client, task_id)
            executed = running.result(timeout=60)

        assert executed["status"] == "canceled"
        # Polling stopped at the cancel: the run never reached a third poll or a download.
        assert len(mock_video.requests(POLL_PATH)) == 2, mock_video.paths()
        assert mock_video.requests(DOWNLOAD_PATH) == []
        assert _assets(client, project["id"], "video") == []


# --- 8. budget ----------------------------------------------------------


def test_video_budget_rejection_sends_nothing_to_the_provider(tmp_path: Path, monkeypatch,
                                                             mock_video) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    with TestClient(create_app(tmp_path / "video_budget.db")) as client:
        project, shots = _project_with_images(client, shot_count=2, budget_limit=1.0)
        _external_video_capability(client, mock_video.url, unit_cost=0.6)
        created = client.post(f"/api/projects/{project['id']}/media-videos")
        assert created.status_code == 201, created.text
        assert created.json()["estimated_cost"] == pytest.approx(1.2)
        executed = _execute(client, created.json()["id"])
        assert executed["status"] == "failed"
        assert "budget limit exceeded" in executed["error"]
        assert any(event["event_type"] == "budget_rejected" for event in executed["events"])
        assert _assets(client, project["id"], "video") == []
        assert mock_video.requests() == []


def test_single_shot_video_task_estimates_one_shot(tmp_path: Path, monkeypatch,
                                                   mock_video) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    mock_video.status_route(POLL_PATH, [{"status": "succeeded", "video_url": DOWNLOAD_PATH}])
    mock_video.payload_route(DOWNLOAD_PATH, VIDEO_BYTES, "video/mp4")
    monkeypatch.setenv("VMAKER_VIDEO_POLL_SECONDS", "0.01")
    with TestClient(create_app(tmp_path / "video_cost.db")) as client:
        project, shots = _project_with_images(client, shot_count=2)
        _external_video_capability(client, mock_video.url, unit_cost=0.25)
        created = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video")
        assert created.status_code == 201, created.text
        assert created.json()["estimated_cost"] == pytest.approx(0.25)
        assert _execute(client, created.json()["id"])["status"] == "completed"
        assert len(mock_video.requests(SUBMIT_PATH)) == 1

        batch = client.post(f"/api/projects/{project['id']}/media-videos")
        assert batch.status_code == 201
        assert batch.json()["estimated_cost"] == pytest.approx(0.5)  # two shots at 0.25 each


# --- 9. connection test -------------------------------------------------


def test_live_video_connection_test_never_generates_a_video(tmp_path: Path, monkeypatch,
                                                            mock_video) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    mock_video.status_route(POLL_PATH, [{"status": "succeeded", "video_url": DOWNLOAD_PATH}])
    mock_video.payload_route(DOWNLOAD_PATH, VIDEO_BYTES, "video/mp4")
    with TestClient(create_app(tmp_path / "video_probe.db")) as client:
        _external_video_capability(client, mock_video.url)
        response = client.post("/api/capability-settings/video/test", json={"live": True})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["capability"] == "video"
        assert result["configured"] is True and result["reachable"] is True
        assert result["live"] is False            # nothing billable was attempted
        assert result["ok"] is True               # ok reflects configured + reachable only
        assert "不做真实生成测试" in result["message"]
        assert "成本高" in result["message"] and "耗时数分钟" in result["message"]
        assert "配置与网络连通性" in result["message"]
        # A "live" test must not reach the generation endpoint at all.
        assert mock_video.requests() == []


# --- 10. upstream errors never leak the key -----------------------------


def test_upstream_http_error_reports_the_status_without_the_key(tmp_path: Path, monkeypatch,
                                                                mock_video) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    # The upstream error body echoes the credential: it must never reach the task record.
    mock_video.error_route(SUBMIT_PATH, 500, f"upstream exploded for key {TEST_API_KEY}")
    with TestClient(create_app(tmp_path / "video_500.db")) as client:
        project, shots = _project_with_images(client, shot_count=1)
        _external_video_capability(client, mock_video.url)
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "failed"
        assert "HTTP 500" in executed["error"]
        assert TEST_API_KEY not in executed["error"]
        assert "upstream exploded" not in executed["error"]
        assert TEST_API_KEY not in json.dumps(executed["events"], ensure_ascii=False)
        assert _assets(client, project["id"], "video") == []


def test_poll_http_error_reports_the_status_without_the_key(tmp_path: Path, monkeypatch,
                                                            mock_video, fast_video_runtime) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    monkeypatch.setenv("VMAKER_VIDEO_TIMEOUT_SECONDS", "0.4")
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    mock_video.error_route(POLL_PATH, 503, f"service unavailable for key {TEST_API_KEY}")
    with TestClient(create_app(tmp_path / "video_poll_503.db")) as client:
        project, shots = _project_with_images(client, shot_count=1)
        _external_video_capability(client, mock_video.url)
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "failed"
        assert "HTTP 503" in executed["error"]
        assert TEST_API_KEY not in executed["error"]
        assert "service unavailable" not in executed["error"]
        assert _assets(client, project["id"], "video") == []


def test_unparsable_submit_response_fails_without_the_key(tmp_path: Path, monkeypatch,
                                                          mock_video) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_API_KEY)
    mock_video.json_route(SUBMIT_PATH, {"unexpected": "shape"})
    with TestClient(create_app(tmp_path / "video_parse.db")) as client:
        project, shots = _project_with_images(client, shot_count=1)
        _external_video_capability(client, mock_video.url)
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "failed"
        assert "无法解析的响应" in executed["error"]
        assert _assets(client, project["id"], "video") == []


def test_missing_video_api_key_environment_variable_fails_honestly(tmp_path: Path, monkeypatch,
                                                                   mock_video) -> None:
    monkeypatch.delenv(TEST_KEY_ENV, raising=False)
    mock_video.json_route(SUBMIT_PATH, {"id": "job-1"})
    with TestClient(create_app(tmp_path / "video_nokey.db")) as client:
        project, shots = _project_with_images(client, shot_count=1)
        configured = _external_video_capability(client, mock_video.url)
        assert configured["configured"] is False
        task = client.post(f"/api/projects/{project['id']}/shots/{shots[0]['id']}/video").json()
        executed = _execute(client, task["id"])
        assert executed["status"] == "failed"
        assert TEST_KEY_ENV in executed["error"]
        assert mock_video.requests() == []
        assert _assets(client, project["id"], "video") == []


# --- request validation -------------------------------------------------


def test_shot_video_request_validation(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "video_validation.db")) as client:
        project, shots = _project_with_shots(client, shot_count=1)
        other, other_shots = _project_with_shots(client, shot_count=1)

        assert client.post("/api/projects/missing/media-videos", json={}).status_code == 404
        assert client.post("/api/projects/missing/shots/shot_missing/video").status_code == 404
        assert client.post(f"/api/projects/{project['id']}/shots/shot_missing/video").status_code == 422
        # A shot that exists but belongs to another project is not this project's shot.
        foreign = client.post(f"/api/projects/{project['id']}/shots/{other_shots[0]['id']}/video")
        assert foreign.status_code == 422
        assert client.post(f"/api/projects/{project['id']}/media-videos", json={"shot_ids": []}).status_code == 422
        assert client.post(f"/api/projects/{project['id']}/media-videos",
                           json={"shot_ids": ["shot_missing"]}).status_code == 422
        assert client.post(f"/api/projects/{project['id']}/media-videos",
                           json={"shot_ids": [shots[0]["id"], shots[0]["id"]]}).status_code == 422
        assert client.post(f"/api/projects/{project['id']}/media-videos",
                           json={"shot_ids": "nope"}).status_code == 422

        created = client.post(f"/api/projects/{project['id']}/media-videos")
        assert created.status_code == 201
        assert created.json()["task_type"] == "shot_video"
        assert created.json()["provider"] == "demo"
        listed = client.get(f"/api/projects/{project['id']}/tasks").json()
        assert created.json()["id"] in [item["id"] for item in listed]

        # An unknown shot media task type is rejected instead of being mapped to another one.
        repo = AnalysisRepository(client.app.state.database)
        task, reason = repo.create_shot_media_task(project["id"], "shot_music", [shots[0]["id"]])
        assert task is None and reason is not None


# --- the prompt builder and the runtime settings ------------------------


def test_shot_video_prompt_describes_motion_over_the_existing_frame() -> None:
    from backend.analysis import shot_video_prompt

    prompt = shot_video_prompt(
        {"visual_style": "水墨动画"},
        {"title": "雨夜天台", "setting": "城市屋顶"},
        {"action": "女孩奔跑", "camera": "low_angle", "narration_dialogue": "我一定会回来。"},
    )
    for fragment in ("水墨动画", "雨夜天台", "low_angle", "女孩奔跑", "我一定会回来。", "第一帧"):
        assert fragment in prompt, fragment
    # Split storyboard camera fields take precedence over the stored single camera string.
    split = shot_video_prompt({}, {}, {"action": "走", "camera": "static", "camera_angle": "俯拍",
                                       "camera_motion": "推轨"})
    assert "俯拍" in split and "推轨" in split and "static" not in split
    fallback = shot_video_prompt(None, None, None)
    assert "未指定视觉风格" in fallback and "固定机位" in fallback


def test_video_runtime_defaults_and_overrides(monkeypatch) -> None:
    """Both video knobs come from the environment with sane defaults, like the worker's."""
    from backend.config import get_settings

    for name in ("VMAKER_VIDEO_POLL_SECONDS", "VMAKER_VIDEO_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    defaults = get_settings()
    assert defaults.video_poll_seconds == pytest.approx(3.0)
    assert defaults.video_timeout_seconds == pytest.approx(600.0)

    monkeypatch.setenv("VMAKER_VIDEO_POLL_SECONDS", "0.5")
    monkeypatch.setenv("VMAKER_VIDEO_TIMEOUT_SECONDS", "0.05")
    overridden = get_settings()
    assert overridden.video_poll_seconds == pytest.approx(0.5)
    # A sub-second total timeout is still a timeout, which is what makes it testable.
    assert overridden.video_timeout_seconds == pytest.approx(0.05)

    for value in ("0", "-3", "abc"):
        monkeypatch.setenv("VMAKER_VIDEO_POLL_SECONDS", value)
        monkeypatch.setenv("VMAKER_VIDEO_TIMEOUT_SECONDS", value)
        fallback = get_settings()
        assert fallback.video_poll_seconds == pytest.approx(3.0), value
        assert fallback.video_timeout_seconds == pytest.approx(600.0), value


def test_video_provider_factory_and_dataclasses() -> None:
    from backend.providers import (
        DemoVideoProvider,
        OpenAICompatibleVideoProvider,
        VideoJob,
        VideoRequest,
        VideoStatus,
        build_video_provider,
    )

    demo = build_video_provider({"provider": "demo"})
    assert isinstance(demo, DemoVideoProvider)
    assert demo.name == "demo" and demo.model == "demo-ffmpeg-v1"
    # 没有 FFmpeg 时它必须拒绝，而不是返回一个空片段。
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MediaRepository, "_ffmpeg", staticmethod(lambda: None))
        with pytest.raises(Exception) as failure:
            demo.submit(VideoRequest(prompt="p", image_bytes=b"x", image_mime="image/png",
                                     duration_sec=5, aspect_ratio="16:9"))
        assert "FFmpeg" in str(failure.value)

    external = build_video_provider({"provider": "kling", "endpoint": "https://api.example.com/v1/",
                                     "model": "kling-v1", "api_key_env": TEST_KEY_ENV})
    assert isinstance(external, OpenAICompatibleVideoProvider)
    assert external.name == "kling" and external.endpoint == "https://api.example.com/v1"
    assert external.timeout == 120 and external.poll_timeout == 30
    # The dataclasses are plain frozen value objects.
    request = VideoRequest("p", b"x", "image/png", 5, "16:9", "static")
    assert request.camera == "static"
    assert VideoJob(provider_job_id="j", payload={"a": 1}).payload == {"a": 1}
    assert VideoStatus(kind="queued").progress is None


def test_video_mime_detection_and_suffixes() -> None:
    from backend.providers import _video_mime_from_magic, suffix_for_mime

    assert _video_mime_from_magic(VIDEO_BYTES) == "video/mp4"
    assert _video_mime_from_magic(b"\x1a\x45\xdf\xa3\x01\x00\x00\x00") == "video/webm"
    assert _video_mime_from_magic(IMAGE_BYTES) is None
    assert suffix_for_mime("video/mp4") == ".mp4"
    assert suffix_for_mime("video/webm; codecs=vp9") == ".webm"
    assert suffix_for_mime("video/unknown") == ".bin"


def test_settings_dataclass_keeps_the_video_knobs() -> None:
    """``Settings`` is frozen and slotted; the new fields must be real, typed members."""
    settings = Settings(database_path=Path("x.db"))
    assert settings.video_poll_seconds == 3.0
    assert settings.video_timeout_seconds == 600.0
    with pytest.raises(Exception):
        settings.video_poll_seconds = 1.0  # type: ignore[misc]
