"""Story-analysis, image, speech, and video providers.

Secrets are read only from the process environment, and every user-visible failure goes
through :func:`scrub_secret` so an upstream service can never echo a key back into a
task error, an API response, or a log line.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.jianying import canvas_for_aspect_ratio
from backend.media import MediaRepository, demo_storyboard_png, demo_storyboard_svg, demo_tone_wav
from backend.schemas import StoryAnalysisResult

IMAGE_TIMEOUT_SECONDS = 120
SPEECH_TIMEOUT_SECONDS = 120
VIDEO_TIMEOUT_SECONDS = 120
# A single poll is a status read, not a generation: it should answer far faster than a submit.
VIDEO_POLL_TIMEOUT_SECONDS = 30
# A single generated image must never be able to exhaust local memory or disk.
MAX_MEDIA_BYTES = 32 * 1024 * 1024
# One generated video is legitimately far larger than a still frame, so it gets its own ceiling.
MAX_VIDEO_BYTES = 256 * 1024 * 1024
# One connection test must never hang the settings screen.
PROBE_TIMEOUT_SECONDS = 5


class ProviderError(RuntimeError):
    """A safe, user-visible provider failure (never contains an API key)."""


class ConnectionCheckError(RuntimeError):
    """A connection-test failure whose message is already safe for the user (Chinese)."""


def scrub_secret(text: object, api_key: str | None) -> str:
    """Remove any occurrence of the secret from arbitrary text.

    Upstream services sometimes quote the presented credential inside an error body, so
    every message is passed through here before it can reach a task record or a response.
    """
    message = str(text)
    if not api_key:
        return message
    for candidate in {api_key, api_key.strip(), urllib.parse.quote(api_key, safe="")}:
        if candidate:
            message = message.replace(candidate, "[REDACTED]")
    return re.sub(r"(?i)(bearer|api[-_]?key|authorization|token)(\s*[:=]?\s*)\S+",
                  r"\1\2[REDACTED]", message)


def _mime_from_magic(data: bytes) -> str | None:
    """Detect PNG / JPEG / WebP from leading bytes only."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize_mime(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).split(";")[0].strip().lower()
    if not normalized or "/" not in normalized:
        return None
    # A generic container type carries no information, so probing the bytes is strictly better.
    if normalized in ("application/octet-stream", "binary/octet-stream", "application/binary"):
        return None
    aliases = {"image/jpg": "image/jpeg", "image/jpe": "image/jpeg", "image/x-png": "image/png"}
    return aliases.get(normalized, normalized)


def _video_mime_from_magic(data: bytes) -> str | None:
    """Detect MP4 / WebM from leading bytes only.

    An MP4 file starts with a big-endian box length followed by ``ftyp``; a WebM file starts
    with the EBML header ``1A 45 DF A3``.
    """
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if len(data) >= 4 and data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    return None


def suffix_for_mime(mime_type: str) -> str:
    normalized = _normalize_mime(mime_type) or ""
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
            "image/svg+xml": ".svg", "audio/wav": ".wav", "audio/x-wav": ".wav",
            "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/ogg": ".ogg",
            "video/mp4": ".mp4", "video/webm": ".webm",
            "video/quicktime": ".mov", "video/x-matroska": ".mkv"}.get(normalized, ".bin")


class StoryAnalyzer(ABC):
    name: str
    model: str

    @abstractmethod
    def analyze(self, story: str) -> StoryAnalysisResult:
        raise NotImplementedError


class DemoAnalyzer(StoryAnalyzer):
    """Explicitly non-AI deterministic analyzer for local development/tests."""

    name = "demo"
    model = "deterministic-v1"

    def analyze(self, story: str) -> StoryAnalysisResult:
        text = " ".join(story.split())
        if not text:
            raise ProviderError("story is empty")
        sentences = [part.strip() for part in re.split(r"[。！？!?\n]+", text) if part.strip()]
        summary = sentences[0][:240]
        if len(sentences) > 1:
            summary = f"{summary}；{sentences[1][:120]}"
        genres = ["本地演示分析"]
        lowered = text.lower()
        if any(word in lowered for word in ("未来", "机器人", "太空", "科幻")):
            genres.append("科幻")
        elif any(word in text for word in ("侦探", "谜", "失踪", "秘密")):
            genres.append("悬疑")
        else:
            genres.append("剧情")
        beat_source = sentences[:5] or [text]
        beats = [
            {"order": index, "title": f"节拍 {index}", "description": sentence[:300]}
            for index, sentence in enumerate(beat_source, start=1)
        ]
        return StoryAnalysisResult.model_validate(
            {
                "summary": summary,
                "genres": genres,
                "audience": ["普通观众", "短视频观众"],
                "characters": [],
                "scenes": [
                    {"title": "主要场景", "setting": "待明确", "description": "根据原故事进一步拆分场景。"}
                ],
                "beats": beats,
                "risks": ["这是确定性的 demo/local 分析，不是 AI 生成结果。"],
                "questions": ["主角的核心目标是什么？", "结局希望给观众何种感受？"],
            }
        )


SYSTEM_PROMPT = """你是影视故事分析助手。仅输出 JSON，字段严格为：summary 字符串；genres 字符串数组；audience 字符串数组；characters 数组（name, role, description）；scenes 数组（title, setting, description）；beats 数组（order 正整数, title, description）；risks 字符串数组；questions 字符串数组。不要输出 Markdown。"""


class OpenAICompatibleAnalyzer(StoryAnalyzer):
    def __init__(self, *, name: str, endpoint: str, model: str, api_key_env: str) -> None:
        self.name = name
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env

    def analyze(self, story: str) -> StoryAnalysisResult:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise ProviderError(f"API key environment variable {self.api_key_env} is not set")
        url = self.endpoint
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": story},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
            return StoryAnalysisResult.model_validate_json(content)
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"provider HTTP error {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError("provider connection failed") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("provider returned an invalid structured response") from exc


def build_analyzer(settings: dict[str, object]) -> StoryAnalyzer:
    provider = str(settings["provider"])
    if provider == "demo":
        return DemoAnalyzer()
    return OpenAICompatibleAnalyzer(
        name=provider,
        endpoint=str(settings["endpoint"]),
        model=str(settings["model"]),
        api_key_env=str(settings["api_key_env"]),
    )


# --------------------------------------------------------------------------------------
# Image generation
# --------------------------------------------------------------------------------------


class ImageProvider(ABC):
    """A single-shot image generator: one call produces one image."""

    name: str
    model: str

    @abstractmethod
    def generate(self, prompt: str, *, size: str = "1280x720") -> tuple[bytes, str]:
        """Return ``(data, mime_type)`` for one generated image."""
        raise NotImplementedError


class DemoImageProvider(ImageProvider):
    """The explicit local placeholder renderer: never claims to be AI-generated."""

    name = "demo"
    model = "demo-bitmap-v1"

    def generate(self, prompt: str, *, size: str = "1280x720") -> tuple[bytes, str]:
        # The placeholder is a fixed-resolution storyboard card; ``size`` is intentionally ignored.
        # It is a bitmap (not SVG) so the default demo media can actually be composited by FFmpeg
        # and imported by 剪映 — neither can read SVG.
        return demo_storyboard_png(1, prompt), "image/png"


class OpenAICompatibleImageProvider(ImageProvider):
    """``POST {endpoint}/images/generations`` with b64_json or url response shapes."""

    def __init__(self, *, name: str, endpoint: str, model: str, api_key_env: str,
                 timeout: int = IMAGE_TIMEOUT_SECONDS) -> None:
        self.name = name
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.timeout = timeout

    def _api_key(self) -> str:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            # Only the variable *name* is ever reported; the value never exists here.
            raise ProviderError(f"图像服务 API key 环境变量 {self.api_key_env} 未设置")
        return api_key

    def generate(self, prompt: str, *, size: str = "1280x720") -> tuple[bytes, str]:
        api_key = self._api_key()
        body = json.dumps(
            {"model": self.model, "prompt": prompt, "n": 1, "size": size, "response_format": "b64_json"},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/images/generations",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ProviderError(scrub_secret(f"图像服务返回 HTTP {exc.code}", api_key)) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(scrub_secret("图像服务连接失败", api_key)) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError(scrub_secret("图像服务返回了无法解析的响应", api_key)) from exc

        try:
            entries = payload["data"]
            if not isinstance(entries, list) or not entries:
                raise TypeError("empty data")
            entry = entries[0]
            if not isinstance(entry, dict):
                raise TypeError("data entry is not an object")
            declared = _normalize_mime(entry.get("output_format") or entry.get("mime_type"))
            if isinstance(entry.get("b64_json"), str) and entry["b64_json"]:
                data = base64.b64decode(entry["b64_json"], validate=False)
                mime = declared or _mime_from_magic(data) or "image/png"
                return self._guard(data, api_key), mime
            if isinstance(entry.get("url"), str) and entry["url"]:
                data, header_mime = self._download(entry["url"], api_key)
                mime = declared or header_mime or _mime_from_magic(data) or "image/png"
                return self._guard(data, api_key), mime
        except ProviderError as exc:
            # Download failures already carry a scrubbed, specific message: keep it.
            raise ProviderError(scrub_secret(str(exc), api_key)) from exc
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error) as exc:
            raise ProviderError(scrub_secret("图像服务返回了无法解析的响应", api_key)) from exc
        # ``data[0]`` carrying raw bytes is a non-standard shape this client refuses to guess at.
        raise ProviderError(scrub_secret("图像服务返回了无法解析的响应", api_key))

    @staticmethod
    def _guard(data: bytes, api_key: str) -> bytes:
        if not data:
            raise ProviderError(scrub_secret("图像服务返回了空图像", api_key))
        if len(data) > MAX_MEDIA_BYTES:
            raise ProviderError(scrub_secret("图像服务返回的数据超过 32 MiB 上限", api_key))
        return data

    def _download(self, url: str, api_key: str) -> tuple[bytes, str | None]:
        """Fetch a returned image URL with the same credential, capped at 32 MiB."""
        # Services may answer with a path relative to the configured base, so resolve it first.
        resolved = urllib.parse.urljoin(f"{self.endpoint}/", url)
        if urllib.parse.urlsplit(resolved).scheme not in ("http", "https"):
            raise ProviderError(scrub_secret("图像服务返回了不支持的下载地址", api_key))
        request = urllib.request.Request(resolved, method="GET", headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read(MAX_MEDIA_BYTES + 1)
                header_mime = _normalize_mime(response.headers.get("Content-Type"))
        except urllib.error.HTTPError as exc:
            raise ProviderError(scrub_secret(f"图像下载返回 HTTP {exc.code}", api_key)) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(scrub_secret("图像下载连接失败", api_key)) from exc
        if len(data) > MAX_MEDIA_BYTES:
            raise ProviderError(scrub_secret("图像下载超过 32 MiB 上限", api_key))
        return data, header_mime


# --------------------------------------------------------------------------------------
# Speech synthesis
# --------------------------------------------------------------------------------------


class SpeechProvider(ABC):
    """A text-to-speech generator: one call produces one audio clip."""

    name: str
    model: str

    @abstractmethod
    def synthesize(self, text: str, *, voice: str = "") -> tuple[bytes, str]:
        """Return ``(audio_bytes, mime_type)`` for one spoken clip."""
        raise NotImplementedError


class DemoSpeechProvider(SpeechProvider):
    """The explicit local tone placeholder: never claims to be synthesized speech."""

    name = "demo"
    model = "demo-tone-v1"

    def synthesize(self, text: str, *, voice: str = "") -> tuple[bytes, str]:
        # Deterministic duration derived from the text length; deliberately a tone, not speech.
        duration = max(1, min(30, len(" ".join(text.split())) // 15 + 1))
        return demo_tone_wav(duration, 1), "audio/wav"


class OpenAICompatibleSpeechProvider(SpeechProvider):
    """``POST {endpoint}/audio/speech`` returning raw audio bytes."""

    def __init__(self, *, name: str, endpoint: str, model: str, api_key_env: str,
                 timeout: int = SPEECH_TIMEOUT_SECONDS) -> None:
        self.name = name
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.timeout = timeout

    def synthesize(self, text: str, *, voice: str = "") -> tuple[bytes, str]:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise ProviderError(f"语音服务 API key 环境变量 {self.api_key_env} 未设置")
        payload: dict[str, Any] = {"model": self.model, "input": text, "response_format": "wav"}
        if voice:
            payload["voice"] = voice
        request = urllib.request.Request(
            f"{self.endpoint}/audio/speech",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read(MAX_MEDIA_BYTES + 1)
                header_mime = _normalize_mime(response.headers.get("Content-Type"))
        except urllib.error.HTTPError as exc:
            raise ProviderError(scrub_secret(f"语音服务返回 HTTP {exc.code}", api_key)) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(scrub_secret("语音服务连接失败", api_key)) from exc
        if len(data) > MAX_MEDIA_BYTES:
            raise ProviderError(scrub_secret("语音服务返回的数据超过 32 MiB 上限", api_key))
        if not data:
            raise ProviderError(scrub_secret("语音服务返回了空音频", api_key))
        return data, header_mime or "audio/wav"


# --------------------------------------------------------------------------------------
# Image-to-video generation
#
# Video is the one capability with no honest local stand-in: a placeholder card cannot become
# a moving picture, so the demo provider fails on purpose instead of writing a fake clip.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoRequest:
    """One image-to-video job request: a still frame plus the motion brief."""

    prompt: str
    image_bytes: bytes
    image_mime: str
    duration_sec: int
    aspect_ratio: str
    camera: str | None = None


@dataclass(frozen=True)
class VideoJob:
    """A submitted asynchronous job.

    ``payload`` carries whatever this provider needs to poll the job later; its shape is
    provider-private so an adapter can keep a request id, a region, or a signed URL in it.
    """

    provider_job_id: str
    payload: dict


@dataclass(frozen=True)
class VideoStatus:
    """One poll answer: ``kind`` is one of queued/processing/succeeded/failed."""

    kind: str
    progress: float | None = None
    message: str | None = None
    video_url: str | None = None


class VideoProvider(ABC):
    """An asynchronous image-to-video service: submit once, then poll until it settles."""

    name: str
    model: str

    @abstractmethod
    def submit(self, request: VideoRequest) -> VideoJob:
        """Start one generation job and return the handle needed to poll it."""
        raise NotImplementedError

    @abstractmethod
    def poll(self, job: VideoJob) -> VideoStatus:
        """Read the current state of a submitted job."""
        raise NotImplementedError

    @abstractmethod
    def fetch(self, job: VideoJob, status: VideoStatus) -> tuple[bytes, str]:
        """Download the finished clip, returning ``(data, mime_type)``."""
        raise NotImplementedError


def render_demo_clip(ffmpeg: str, request: VideoRequest) -> bytes:
    """Render a real MP4 placeholder from the shot's own image, using the local FFmpeg.

    The moving marker is deliberate: it makes the clip obviously synthetic at a glance, so a
    placeholder can never be mistaken for a generated shot. The canvas follows the project's aspect
    ratio, using the same mapping as the MP4 export and the 剪映 draft so all three agree.
    """
    width, height = canvas_for_aspect_ratio(request.aspect_ratio)[0]
    duration = max(1, int(request.duration_sec))
    marker_width = max(80, width // 8)
    with tempfile.TemporaryDirectory(prefix="vmaker-demo-clip-") as workspace:
        frame = Path(workspace) / "frame.png"
        frame.write_bytes(request.image_bytes)
        output = Path(workspace) / "clip.mp4"
        filters = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"drawbox=x='mod(t*{max(1, width // 4)},{width})':y={height - 70}:"
            f"w={marker_width}:h=22:color=yellow@0.85:t=fill,"
            f"format=yuv420p"
        )
        completed = subprocess.run(
            [ffmpeg, "-y", "-loop", "1", "-i", str(frame), "-t", str(duration), "-r", "30",
             "-vf", filters, "-an", "-c:v", "libx264", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise ProviderError(
                "本地占位视频渲染失败（FFmpeg 退出码 "
                f"{completed.returncode}）：{(completed.stderr or b'').decode('utf-8', 'replace').strip()[-300:]}"
            )
        return output.read_bytes()


class DemoVideoProvider(VideoProvider):
    """A local placeholder clip rendered with FFmpeg — no generation service involved.

    There is still no way to *generate* a shot locally. But "a placeholder cannot be a real clip"
    stopped being true once FFmpeg was available: the shot's own placeholder image can be turned
    into a real, decodable MP4 carrying an unmistakable synthetic marker. That keeps the video
    capability exercisable end to end (timeline, MP4 export, 剪映 draft) without paying a service,
    and the result is labelled a placeholder in its metadata, not passed off as generation.

    Without FFmpeg there is nothing honest to return, so it fails with an actionable reason.
    """

    name = "demo"
    model = "demo-ffmpeg-v1"

    def submit(self, request: VideoRequest) -> VideoJob:
        ffmpeg = MediaRepository._ffmpeg()
        if not ffmpeg:
            raise ProviderError(
                "视频能力既没有配置真实服务，也没有可用的 FFmpeg 来生成本地占位视频。"
                "请安装可选项 imageio-ffmpeg（或把 ffmpeg 放进 PATH / 设置 VMAKER_FFMPEG_PATH），"
                "或在「模型设置 → 视频」中配置真实的图生视频服务。"
            )
        data = render_demo_clip(ffmpeg, request)
        return VideoJob(provider_job_id="demo-local", payload={"data": data, "mime": "video/mp4"})

    def poll(self, job: VideoJob) -> VideoStatus:
        # 本地渲染在 submit 里已经完成，没有异步状态可查。
        return VideoStatus(kind="succeeded", progress=1.0, message="本地占位视频已生成")

    def fetch(self, job: VideoJob, status: VideoStatus) -> tuple[bytes, str]:
        return job.payload["data"], str(job.payload["mime"])


class OpenAICompatibleVideoProvider(VideoProvider):
    """A submit-then-poll image-to-video client for V-Maker's **own** conventions.

    .. warning::

       This is **V-Maker's own convention, not any vendor's standard API**. No real service
       (可灵 / 火山 / Runway / …) is expected to answer these paths and shapes as-is. Adapting a
       real vendor means writing a new provider from its documentation, using this class only as
       a shape reference: ``submit`` starts a job, ``poll`` maps one status document onto
       :class:`VideoStatus`, and ``fetch`` downloads the resulting bytes.

    The convention itself is::

        POST {endpoint}/video/generations
            {"model","prompt","image_base64","image_mime_type",
             "duration_sec","aspect_ratio","camera"}
            -> {"id" | "task_id": "..."}

        GET  {endpoint}/video/generations/{id}
            -> {"status":"queued|processing|succeeded|failed",
                "progress":0.42,"message":"...","video_url":"...","error":"..."}

        GET  {video_url}   (same Authorization header)  -> the clip bytes

    Every failure becomes a :class:`ProviderError` whose message has been through
    :func:`scrub_secret`, and the unknown-status cases are never silently reported as success.
    """

    def __init__(self, *, name: str, endpoint: str, model: str, api_key_env: str,
                 timeout: int = VIDEO_TIMEOUT_SECONDS,
                 poll_timeout: int = VIDEO_POLL_TIMEOUT_SECONDS) -> None:
        self.name = name
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.poll_timeout = poll_timeout

    # --- credential and transport helpers ----------------------------------------------

    def _api_key(self) -> str:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            # Only the variable *name* is ever reported; the value never exists here.
            raise ProviderError(f"视频服务 API key 环境变量 {self.api_key_env} 未设置")
        return api_key

    def _clean(self, text: object, api_key: str) -> str:
        return scrub_secret(text, api_key)

    def _json_request(self, url: str, api_key: str, *, payload: dict[str, Any] | None,
                      timeout: int) -> dict[str, Any]:
        """One JSON call whose every failure is already a safe, scrubbed Chinese message."""
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST" if data is not None else "GET",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            # The upstream body is never echoed: it can quote the credential back at us.
            raise ProviderError(self._clean(f"视频服务返回 HTTP {exc.code}", api_key)) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(self._clean("视频服务连接失败", api_key)) from exc
        except (TimeoutError, OSError) as exc:
            raise ProviderError(self._clean("视频服务连接超时", api_key)) from exc
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError(self._clean("视频服务返回了无法解析的响应", api_key)) from exc
        if not isinstance(decoded, dict):
            raise ProviderError(self._clean("视频服务返回了无法解析的响应", api_key))
        return decoded

    # --- VideoProvider contract ---------------------------------------------------------

    def submit(self, request: VideoRequest) -> VideoJob:
        api_key = self._api_key()
        payload = {
            "model": self.model,
            "prompt": request.prompt,
            "image_base64": base64.b64encode(request.image_bytes).decode("ascii"),
            "image_mime_type": request.image_mime,
            "duration_sec": int(request.duration_sec),
            "aspect_ratio": request.aspect_ratio,
            "camera": request.camera,
        }
        answer = self._json_request(f"{self.endpoint}/video/generations", api_key,
                                    payload=payload, timeout=self.timeout)
        job_id = answer.get("id") or answer.get("task_id")
        if not isinstance(job_id, str) or not job_id.strip():
            # A 200 without a usable handle is not a submitted job, whatever else it contains.
            raise ProviderError(self._clean("视频服务返回了无法解析的响应", api_key))
        return VideoJob(provider_job_id=job_id.strip(), payload={"model": self.model})

    def poll(self, job: VideoJob) -> VideoStatus:
        api_key = self._api_key()
        answer = self._json_request(
            f"{self.endpoint}/video/generations/{urllib.parse.quote(job.provider_job_id, safe='')}",
            api_key, payload=None, timeout=self.poll_timeout)
        raw_status = str(answer.get("status") or "").strip().lower()
        progress = answer.get("progress")
        try:
            progress_value = None if progress is None else max(0.0, min(1.0, float(progress)))
        except (TypeError, ValueError):
            progress_value = None
        message = answer.get("message")
        message_text = str(message).strip() if message else ""
        if raw_status in ("queued", "processing", "succeeded", "failed"):
            kind = raw_status
        else:
            # An unknown status is genuinely unknown: reporting it as processing keeps the run
            # alive, and the raw value stays visible instead of being laundered into progress.
            kind = "processing"
            unknown = f"视频服务返回了未知状态：{raw_status or '(空)'}"
            message_text = f"{unknown}；{message_text}" if message_text else unknown
        video_url = answer.get("video_url")
        if not isinstance(video_url, str) or not video_url.strip():
            video_url = None
        error = answer.get("error")
        if kind == "failed":
            failure = str(error).strip() if error else ""
            if not failure:
                failure = message_text or "视频服务报告任务失败，但没有给出原因"
            message_text = self._clean(failure, api_key)
        return VideoStatus(kind=kind, progress=progress_value,
                           message=message_text or None, video_url=video_url)

    def fetch(self, job: VideoJob, status: VideoStatus) -> tuple[bytes, str]:
        api_key = self._api_key()
        if not status.video_url:
            raise ProviderError(self._clean("视频服务已完成，但没有返回视频地址", api_key))
        # Services may answer with a path relative to the configured base, so resolve it first.
        resolved = urllib.parse.urljoin(f"{self.endpoint}/", status.video_url)
        if urllib.parse.urlsplit(resolved).scheme not in ("http", "https"):
            raise ProviderError(self._clean("视频服务返回了不支持的下载地址", api_key))
        request = urllib.request.Request(resolved, method="GET",
                                         headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read(MAX_VIDEO_BYTES + 1)
                header_mime = _normalize_mime(response.headers.get("Content-Type"))
        except urllib.error.HTTPError as exc:
            raise ProviderError(self._clean(f"视频下载返回 HTTP {exc.code}", api_key)) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(self._clean("视频下载连接失败", api_key)) from exc
        except (TimeoutError, OSError) as exc:
            raise ProviderError(self._clean("视频下载连接超时", api_key)) from exc
        if len(data) > MAX_VIDEO_BYTES:
            raise ProviderError(self._clean("视频下载超过 256 MiB 上限", api_key))
        if not data:
            raise ProviderError(self._clean("视频服务返回了空视频", api_key))
        mime = header_mime or _video_mime_from_magic(data) or "video/mp4"
        return data, mime


# --------------------------------------------------------------------------------------
# Factories
# --------------------------------------------------------------------------------------


def build_image_provider(settings: dict[str, object]) -> ImageProvider:
    provider = str(settings["provider"])
    if provider == "demo":
        return DemoImageProvider()
    return OpenAICompatibleImageProvider(
        name=provider,
        endpoint=str(settings.get("endpoint") or ""),
        model=str(settings.get("model") or ""),
        api_key_env=str(settings.get("api_key_env") or ""),
    )


def build_speech_provider(settings: dict[str, object]) -> SpeechProvider:
    provider = str(settings["provider"])
    if provider == "demo":
        return DemoSpeechProvider()
    return OpenAICompatibleSpeechProvider(
        name=provider,
        endpoint=str(settings.get("endpoint") or ""),
        model=str(settings.get("model") or ""),
        api_key_env=str(settings.get("api_key_env") or ""),
    )


def build_video_provider(settings: dict[str, object]) -> VideoProvider:
    """``demo`` means "no video at all", which is why its provider refuses instead of faking one."""
    provider = str(settings["provider"])
    if provider == "demo":
        return DemoVideoProvider()
    return OpenAICompatibleVideoProvider(
        name=provider,
        endpoint=str(settings.get("endpoint") or ""),
        model=str(settings.get("model") or ""),
        api_key_env=str(settings.get("api_key_env") or ""),
    )


# --------------------------------------------------------------------------------------
# Connection testing
#
# These helpers exist so the settings screen can answer "would this work?" before the user
# pays for anything. They are deliberately separate from the generation providers: the
# reachability probe never authenticates and never triggers a billable job, and the live
# checks below send the smallest request each service accepts.
# --------------------------------------------------------------------------------------


def probe_endpoint(host: str, port: int, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """Open and close one TCP connection to ``host:port``.

    This is the whole reachability contract: it proves the address answers on that port and
    nothing else. No request line, no headers, and above all no credential is ever sent.
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def endpoint_address(endpoint: str) -> tuple[str, int] | None:
    """Split an endpoint into the ``(host, port)`` pair the TCP probe needs.

    ``None`` means the endpoint is missing or is not an ``http(s)`` URL, which the caller
    reports honestly instead of pretending the network was verified.
    """
    try:
        parts = urllib.parse.urlsplit(str(endpoint or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return parts.hostname, port


def connect_endpoint(endpoint: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Probe one endpoint URL, returning ``(reachable, safe_detail)``.

    Failing to *parse* and failing to *connect* are different problems for the user, so they
    get different explanations even though both end as ``reachable=false``.
    """
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return False, "缺少 endpoint"
    address = endpoint_address(endpoint)
    if address is None:
        return False, "endpoint 不是可识别的 http(s) 地址"
    host, port = address
    if probe_endpoint(host, port, timeout):
        return True, ""
    return False, f"无法连接 {host}:{port}"


def _provider_url(endpoint: str, suffix: str) -> str:
    """Join an endpoint with one API path without duplicating an already-present suffix."""
    base = str(endpoint or "").strip()
    if not base:
        return suffix
    base = base.rstrip("/")
    suffix_path = suffix.lstrip("/")
    parts = urllib.parse.urlsplit(base)
    path = parts.path.rstrip("/")
    if path.endswith("/" + suffix_path) or path == "/" + suffix_path:
        # The endpoint already names this exact operation: calling it is not an error.
        return base
    if path.endswith("/" + suffix_path.rsplit("/", 1)[0]):
        # The endpoint points at the operation's collection, e.g. ".../v1/audio".
        return f"{base}/{suffix_path.rsplit('/', 1)[1]}"
    return f"{base}/{suffix_path}"


def _live_request(url: str, api_key: str, payload: dict[str, Any], timeout: float) -> bytes:
    """POST one authenticated JSON request, translating every failure into a safe Chinese error."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(1024)
    except urllib.error.HTTPError as exc:
        # The upstream body is never echoed: it can quote the credential back at us.
        raise ConnectionCheckError(f"服务返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ConnectionCheckError("调用服务失败：连接错误或超时") from exc
    except (TimeoutError, OSError) as exc:
        raise ConnectionCheckError("调用服务失败：连接错误或超时") from exc


def test_text_connection(*, endpoint: str, model: str, api_key: str,
                         timeout: float = 60) -> str:
    """One minimal chat completion: the cheapest honest proof that text generation works."""
    url = _provider_url(endpoint, "/chat/completions")
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    try:
        _live_request(url, api_key, payload, timeout)
    except ConnectionCheckError as exc:
        raise ConnectionCheckError(f"文本服务真实调用失败：{exc}") from exc
    return "文本服务真实调用成功（一次最小 chat/completions 请求）"


def test_speech_connection(*, endpoint: str, model: str, api_key: str,
                           timeout: float = 60) -> str:
    """One minimal synthesis of a single punctuation mark: the cheapest audible proof."""
    url = _provider_url(endpoint, "/audio/speech")
    payload = {"model": model, "input": "。", "response_format": "wav"}
    try:
        _live_request(url, api_key, payload, timeout)
    except ConnectionCheckError as exc:
        raise ConnectionCheckError(f"语音服务真实调用失败：{exc}") from exc
    return "语音服务真实调用成功（一句最小 audio/speech 请求）"
