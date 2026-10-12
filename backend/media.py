"""Honest local demo media, neutral timeline, and export implementation."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import wave
import zipfile
import zlib
from pathlib import Path
from uuid import uuid4

from backend.config import get_settings
from backend.database import Database
from backend.jianying import (
    LIBRARY_NAME as JIANYING_LIBRARY_NAME,
    build_jianying_draft,
    canvas_for_aspect_ratio,
    library_available as jianying_library_available,
    library_version as jianying_library_version,
)
from backend.repository import utc_now

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

#: Accepted values of ``create_export(format=...)``.
EXPORT_FORMATS: tuple[str, ...] = ("auto", "mp4", "portable_draft", "jianying_draft")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# FFmpeg 能直接解码的位图格式；SVG 等矢量格式不在其中（没有内置 SVG 解码器）。
RASTER_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif")


def _with_note(prefix: str, note: str | None) -> str:
    """Append a package note (skipped files) to a warning, keeping it impossible to miss."""
    return f"{prefix}；{note}" if note else prefix


def _stderr_tail(raw: bytes | str | None, lines: int = 3, limit: int = 400) -> str:
    """Last few lines of FFmpeg stderr: enough to be actionable, short enough for a warning.

    Callers use both ``text=True`` and byte capture, so accept either.
    """
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = raw or ""
    text = text.strip()
    if not text:
        return "（FFmpeg 未输出错误信息）"
    tail = " / ".join(text.splitlines()[-lines:])
    return tail[:limit]


#: 产物时长允许的偏差（秒）：容器时间戳与逐镜头编码会有毫秒级舍入。
MP4_DURATION_TOLERANCE_SEC = 1.5


def probe_media(ffmpeg: str, path: Path) -> dict[str, object]:
    """Read a media file's real streams back out of FFmpeg.

    ``ffprobe`` is not part of the optional ``imageio-ffmpeg`` wheel, and asking FFmpeg to describe
    its own input costs one cheap process — so verification does not depend on a second binary.
    """
    completed = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                               capture_output=True, text=True, timeout=120)
    text = completed.stderr or ""
    duration = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    seconds = None
    if duration:
        seconds = int(duration.group(1)) * 3600 + int(duration.group(2)) * 60 + float(duration.group(3))
    size = re.search(r"Video:[^\n]*?, (\d+)x(\d+)", text)
    return {
        "duration_sec": seconds,
        "width": int(size.group(1)) if size else None,
        "height": int(size.group(2)) if size else None,
        "has_video": "Video:" in text,
        "has_audio": "Audio:" in text,
    }


def verify_mp4_output(ffmpeg: str, path: Path, *, expected_duration_sec: float,
                      expected_canvas: tuple[int, int]) -> dict[str, object]:
    """Prove the composited MP4 is really a complete, playable file of the expected shape.

    A successful FFmpeg exit code is not proof: a truncated or half-written container still exists
    on disk. Everything below therefore fails loudly with an actionable reason instead of letting
    the export report a clean ``completed`` for a file nobody can play.
    """
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("合成后的 MP4 不存在或是空文件")
    with path.open("rb") as handle:
        head = handle.read(12)
    if head[4:8] != b"ftyp":
        raise ValueError(f"合成产物不是 MP4 容器（文件头 {head[:12]!r}）")

    # 完整解码一遍：只读元数据无法发现中途截断或损坏的帧。
    decode = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
                            capture_output=True, text=True, timeout=1800)
    noise = (decode.stderr or "").strip()
    if decode.returncode != 0 or noise:
        raise ValueError(f"合成后的 MP4 无法完整解码：{_stderr_tail(decode.stderr)}")

    probe = probe_media(ffmpeg, path)
    if not probe["has_video"]:
        raise ValueError("合成后的 MP4 没有视频流")
    if not probe["has_audio"]:
        raise ValueError("合成后的 MP4 没有音频流")
    width, height = expected_canvas
    if (probe["width"], probe["height"]) != (width, height):
        raise ValueError(f"成片画布 {probe['width']}x{probe['height']} 与项目宽高比要求的 "
                         f"{width}x{height} 不一致")
    measured = probe["duration_sec"]
    if measured is None:
        raise ValueError("FFmpeg 读不出成片时长，无法确认它是完整的")
    if abs(float(measured) - float(expected_duration_sec)) > MP4_DURATION_TOLERANCE_SEC:
        raise ValueError(f"成片时长 {measured:.2f}s 与时间线 {expected_duration_sec:.2f}s 相差过大")
    return probe


def demo_storyboard_svg(number: int, text: str) -> bytes:
    """The honest local storyboard placeholder as SVG.

    Kept because SVG is exactly the case the **Jianying exporter has to skip** and the MP4 renderer
    has to refuse — tests use it to build that fixture. The default demo media is a bitmap now (see
    :func:`demo_storyboard_png`), because neither FFmpeg nor 剪映 can decode SVG.
    """
    safe = html.escape(" ".join(text.split())[:100])
    return (f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
<rect width="1280" height="720" fill="#172033"/><rect x="36" y="36" width="1208" height="648" rx="24" fill="#26344f" stroke="#8aa4d6" stroke-width="4"/>
<text x="640" y="245" text-anchor="middle" fill="#ffcc66" font-family="sans-serif" font-size="86" font-weight="bold">DEMO STORYBOARD</text>
<text x="640" y="380" text-anchor="middle" fill="white" font-family="sans-serif" font-size="72">SHOT {number}</text>
<text x="640" y="485" text-anchor="middle" fill="#d8e3fa" font-family="sans-serif" font-size="30">{safe}</text>
<text x="640" y="625" text-anchor="middle" fill="#ff9f9f" font-family="sans-serif" font-size="25">LOCAL PLACEHOLDER — NOT AI-GENERATED MEDIA</text>
<text x="640" y="665" text-anchor="middle" fill="#ffcc66" font-family="sans-serif" font-size="24">DEMO / NOT AI-GENERATED</text>
</svg>''').encode("utf-8")


#: 5x7 点阵字模。离线渲染中文需要字体引擎，所以占位图只用点阵画拉丁字符；
#: 镜头自己的文字仍然保留在素材元数据与界面上，不会被丢掉。
_FONT_5X7: dict[str, tuple[str, ...]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    " ": ("00000",) * 7,
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
}

#: 七色测试卡条纹：一眼就能看出这是占位素材，而不是生成结果。
_COLOR_BARS = ((192, 192, 192), (192, 192, 0), (0, 192, 192), (0, 192, 0),
               (192, 0, 192), (192, 0, 0), (0, 0, 192))


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _encode_png(width: int, height: int, pixels: bytearray) -> bytes:
    """Minimal 8-bit RGB PNG writer — stdlib only, so the demo placeholder stays dependency-free."""
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)  # filter type 0 (None)
        raw += pixels[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _png_chunk(b"IEND", b""))


def _fill_rect(pixels: bytearray, width: int, height: int, x: int, y: int, rect_w: int, rect_h: int,
               color: tuple[int, int, int]) -> None:
    left, right = max(0, x), min(width, x + rect_w)
    top, bottom = max(0, y), min(height, y + rect_h)
    span = bytes(color)
    for row in range(top, bottom):
        start = (row * width + left) * 3
        pixels[start:start + (right - left) * 3] = span * (right - left)


def _draw_text(pixels: bytearray, width: int, height: int, text: str, x: int, y: int, scale: int,
               color: tuple[int, int, int]) -> int:
    """Draw 5x7 glyphs left to right; returns the width used. Unknown characters are skipped."""
    cursor = x
    for character in text.upper():
        glyph = _FONT_5X7.get(character)
        if glyph is None:
            cursor += 6 * scale
            continue
        for row, line in enumerate(glyph):
            for column, dot in enumerate(line):
                if dot == "1":
                    _fill_rect(pixels, width, height, cursor + column * scale, y + row * scale, scale, scale, color)
        cursor += 6 * scale
    return cursor - x


def _text_width(text: str, scale: int) -> int:
    return len(text) * 6 * scale - scale


def demo_storyboard_png(number: int, text: str = "") -> bytes:
    """The honest local storyboard placeholder as a **bitmap** (1280x720 PNG).

    The demo image must be a bitmap, not SVG: FFmpeg has no SVG decoder and 剪映 cannot import it,
    so an SVG placeholder makes both exports impossible on a project that has nothing else. This is
    the default demo media, therefore it decides whether an out-of-the-box project can produce a
    real MP4 at all.

    ``text`` is accepted for call-site compatibility but is deliberately not rasterised: offline
    text rendering of arbitrary (mostly CJK) captions needs a font engine. The placeholder instead
    carries colour bars, a shot number and an explicit NOT-AI notice, and the shot's own text stays
    in the asset metadata and the UI.
    """
    width, height = 1280, 720
    pixels = bytearray(width * height * 3)
    bar_width = width // len(_COLOR_BARS)
    for index, color in enumerate(_COLOR_BARS):
        _fill_rect(pixels, width, height, index * bar_width, 0,
                   bar_width if index < len(_COLOR_BARS) - 1 else width - index * bar_width, height, color)
    # 中间压一块深色面板，让文字在任何条纹上都读得清。版面按面板高度排布，页脚不能压到边框上。
    panel_top, panel_height = 100, 540
    _fill_rect(pixels, width, height, 60, panel_top, width - 120, panel_height, (23, 32, 51))
    _fill_rect(pixels, width, height, 60, panel_top, width - 120, 6, (138, 164, 214))
    _fill_rect(pixels, width, height, 60, panel_top + panel_height - 6, width - 120, 6, (138, 164, 214))

    def centered(label: str, top: int, scale: int, color: tuple[int, int, int]) -> None:
        _draw_text(pixels, width, height, label, (width - _text_width(label, scale)) // 2, top, scale, color)

    ink, accent, alert = (255, 255, 255), (255, 204, 102), (255, 159, 159)
    centered("DEMO STORYBOARD", 140, 7, accent)
    centered(f"SHOT {number}", 240, 16, ink)
    centered("NOT AI GENERATED", 420, 6, alert)
    centered("LOCAL PLACEHOLDER - NOT AI-GENERATED MEDIA", 520, 4, accent)
    return _encode_png(width, height, pixels)


def demo_tone_wav(duration: int, number: int) -> bytes:
    """A short audible marker followed by silence, reused by the demo speech provider."""
    duration = max(1, int(duration))
    rate, amplitude, frequency = 8000, 5000, 440 + (number % 4) * 110
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, rate, duration * rate, "NONE", "not compressed"))
        frames = bytearray()
        # Short audible marker, then silence: deliberately a placeholder, not synthetic speech.
        marker = min(rate // 5, duration * rate)
        for i in range(duration * rate):
            sample = amplitude if i < marker and ((i * frequency // rate) % 2 == 0) else (-amplitude if i < marker else 0)
            frames.extend(struct.pack("<h", sample))
        wav.writeframes(bytes(frames))
    return output.getvalue()


class MediaRepository:
    """Stores immutable media versions below one project-scoped local directory."""

    def __init__(self, database: Database, projects_root: Path | str | None = None) -> None:
        self.database = database
        self.projects_root = Path(projects_root) if projects_root else database.path.parent / "projects"

    def _project_dir(self, project_id: str) -> Path:
        if not _SAFE_ID.fullmatch(project_id):
            raise ValueError("unsafe project id")
        root = self.projects_root.resolve()
        target = (root / project_id).resolve()
        if target.parent != root:
            raise ValueError("unsafe project path")
        return target

    def _resolve_relative(self, project_id: str, relative_path: str) -> Path:
        root = self._project_dir(project_id)
        path = (root / relative_path).resolve()
        if path != root and root not in path.parents:
            raise ValueError("unsafe relative path")
        return path

    def ensure_project(self, project_id: str) -> bool:
        try:
            project_dir = self._project_dir(project_id)
        except ValueError:
            return False
        with self.database.connect() as c:
            exists = c.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is not None
        if exists:
            for name in ("assets", "timelines", "exports"):
                (project_dir / name).mkdir(parents=True, exist_ok=True)
        return exists

    @staticmethod
    def _asset(row, versions: list[dict] | None = None) -> dict:
        result = dict(row)
        if versions is not None:
            for version in versions:
                version["demo"] = bool(version["demo"])
                version["metadata"] = json.loads(version.pop("metadata_json"))
            result["versions"] = versions
            result["latest_version"] = versions[-1] if versions else None
        return result

    def list_assets(self, project_id: str) -> list[dict] | None:
        if not self.ensure_project(project_id):
            return None
        with self.database.connect() as c:
            assets = c.execute("SELECT * FROM assets WHERE project_id=? ORDER BY created_at,id", (project_id,)).fetchall()
            result = []
            for row in assets:
                versions = [dict(v) for v in c.execute(
                    "SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version", (row["id"],)
                ).fetchall()]
                result.append(self._asset(row, versions))
        return result

    def _register_bytes(self, c, project_id: str, kind: str, source_type: str, data: bytes,
                        suffix: str, mime_type: str, source_shot_id: str | None = None,
                        source_task_id: str | None = None, demo: bool = False,
                        metadata: dict | None = None) -> dict:
        asset_id, version_id, now = _id("asset"), _id("aver"), utc_now()
        relative = f"assets/{asset_id}/v1{suffix}"
        path = self._resolve_relative(project_id, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = _sha(data)
        c.execute("INSERT INTO assets (id,project_id,kind,source_type,source_shot_id,source_task_id,created_at) VALUES (?,?,?,?,?,?,?)",
                  (asset_id, project_id, kind, source_type, source_shot_id, source_task_id, now))
        c.execute("INSERT INTO asset_versions (id,asset_id,version,sha256,mime_type,relative_path,byte_size,demo,metadata_json,created_at) VALUES (?,?,1,?,?,?,?,?,?,?)",
                  (version_id, asset_id, digest, mime_type, relative, len(data), int(demo),
                   json.dumps(metadata or {}, ensure_ascii=False), now))
        return {"id": asset_id, "project_id": project_id, "kind": kind, "source_type": source_type,
                "source_shot_id": source_shot_id, "source_task_id": source_task_id, "created_at": now,
                "versions": [{"id": version_id, "asset_id": asset_id, "version": 1, "sha256": digest,
                              "mime_type": mime_type, "relative_path": relative, "byte_size": len(data),
                              "demo": demo, "metadata": metadata or {}, "created_at": now}]}

    @staticmethod
    def _svg(number: int, text: str) -> bytes:
        # Kept as a thin forwarder so existing callers and tests stay valid.
        return demo_storyboard_svg(number, text)

    @staticmethod
    def _png(number: int, text: str) -> bytes:
        return demo_storyboard_png(number, text)

    @staticmethod
    def _wav(duration: int, number: int) -> bytes:
        return demo_tone_wav(duration, number)

    @staticmethod
    def _srt_time(seconds: int) -> str:
        return f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02},000"

    def generate_demo_media(self, project_id: str, source_task_id: str | None = None) -> dict | None:
        if not self.ensure_project(project_id):
            return None
        now = utc_now()
        with self.database.connect() as c:
            shots = c.execute("""SELECT sh.*,sc.position scene_position,e.position episode_position
                FROM shots sh JOIN scenes sc ON sc.id=sh.scene_id JOIN episodes e ON e.id=sc.episode_id
                WHERE e.project_id=? AND e.version=(SELECT MAX(version) FROM episodes WHERE project_id=?)
                ORDER BY e.position,sc.position,sh.position""", (project_id, project_id)).fetchall()
            if not shots:
                raise ValueError("shots required")
            created, srt_lines, elapsed = [], [], 0
            for number, shot in enumerate(shots, 1):
                label = shot["action"] or shot["narration_dialogue"] or "Demo shot"
                # 占位图必须是位图：FFmpeg 没有 SVG 解码器、剪映也不能导入 SVG。demo 是默认素材，
                # 用 SVG 会让「开箱即用的项目」两条导出路径都出不了东西。
                created.append(self._register_bytes(c, project_id, "image", "demo", self._png(number, label),
                    ".png", "image/png", shot["id"], source_task_id=source_task_id, demo=True,
                    metadata={"notice": "demo bitmap placeholder; not AI-generated",
                              "shot_number": number, "caption": label[:200]}))
                created.append(self._register_bytes(c, project_id, "audio", "demo", self._wav(int(shot["duration_sec"]), number),
                    ".wav", "audio/wav", shot["id"], source_task_id=source_task_id, demo=True,
                    metadata={"notice": "demo tone/silence placeholder; not TTS or AI speech", "duration_sec": shot["duration_sec"]}))
                end = elapsed + int(shot["duration_sec"])
                text = " ".join((shot["narration_dialogue"] or label).split())[:160]
                srt_lines.extend([str(number), f"{self._srt_time(elapsed)} --> {self._srt_time(end)}", f"[DEMO] {text}", ""])
                elapsed = end
            subtitle = self._register_bytes(c, project_id, "subtitle", "demo", "\n".join(srt_lines).encode("utf-8"),
                ".srt", "application/x-subrip", source_task_id=source_task_id, demo=True,
                metadata={"notice": "demo subtitles derived from shot text; not AI-generated"})
            created.append(subtitle)
            c.execute("UPDATE workflow_stages SET status='completed',updated_at=? WHERE project_id=? AND stage_key IN ('assets','audio')", (now, project_id))
        return {"project_id": project_id, "demo": True, "notice": "Local placeholders only; no real AI media was generated.", "assets": created}

    def asset_version(self, project_id: str, asset_id: str, version_id: str) -> tuple[Path, str, str, str, int] | None:
        """Resolve one registered asset version, re-verifying its checksum before serving it."""
        with self.database.connect() as c:
            project = c.execute("SELECT 1 FROM projects WHERE id=? AND deleted_at IS NULL", (project_id,)).fetchone()
            if not project:
                return None
            asset = c.execute("SELECT id,project_id FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not asset or str(asset["project_id"]) != project_id:
                return None
            version = c.execute("SELECT * FROM asset_versions WHERE id=? AND asset_id=?", (version_id, asset_id)).fetchone()
            if not version:
                return None
            relative = str(version["relative_path"])
            stored_sha = str(version["sha256"])
            mime_type = str(version["mime_type"])
            byte_size = int(version["byte_size"])
            number = int(version["version"])
        try:
            path = self._resolve_relative(project_id, relative)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            if _sha(path.read_bytes()) != stored_sha:
                return None
        except OSError:
            return None
        suffix = Path(relative).suffix
        return path, mime_type, f"{asset_id}_v{number}{suffix}", stored_sha, byte_size

    def create_timeline(self, project_id: str) -> dict | None:
        assets = self.list_assets(project_id)
        if assets is None:
            return None
        latest_by_shot: dict[tuple[str, str], dict] = {}
        subtitle = None
        for asset in assets:
            latest = asset["latest_version"]
            if not latest:
                continue
            if asset["source_shot_id"] and asset["kind"] in ("image", "audio"):
                latest_by_shot[(asset["source_shot_id"], asset["kind"])] = {"asset_id": asset["id"], "asset_version_id": latest["id"]}
            elif asset["kind"] == "subtitle":
                subtitle = {"asset_id": asset["id"], "asset_version_id": latest["id"]}
        with self.database.connect() as c:
            shots = c.execute("""SELECT sh.id,sh.duration_sec,sh.action FROM shots sh JOIN scenes sc ON sc.id=sh.scene_id
                JOIN episodes e ON e.id=sc.episode_id WHERE e.project_id=? AND e.version=(SELECT MAX(version) FROM episodes WHERE project_id=?)
                ORDER BY e.position,sc.position,sh.position""", (project_id, project_id)).fetchall()
            if not shots:
                raise ValueError("shots required")
            missing = [s["id"] for s in shots if (s["id"], "image") not in latest_by_shot or (s["id"], "audio") not in latest_by_shot]
            if missing:
                raise ValueError("demo media required for every shot")
            cursor, clips = 0, []
            for position, shot in enumerate(shots, 1):
                duration = int(shot["duration_sec"])
                clips.append({"position": position, "shot_id": shot["id"], "start_sec": cursor,
                              "duration_sec": duration, "image": latest_by_shot[(shot["id"], "image")],
                              "audio": latest_by_shot[(shot["id"], "audio")], "label": shot["action"]})
                cursor += duration
            timeline = {"schema": "vmaker.neutral-timeline/v1", "project_id": project_id,
                        "duration_sec": cursor, "tracks": {"video_audio": clips, "subtitles": subtitle}}
            encoded = _json_bytes(timeline); version = c.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM timeline_versions WHERE project_id=?", (project_id,)).fetchone()[0]
            timeline_id, now = _id("timeline"), utc_now()
            relative = f"timelines/v{version}.json"; path = self._resolve_relative(project_id, relative)
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(encoded)
            c.execute("INSERT INTO timeline_versions (id,project_id,version,timeline_json,sha256,created_at) VALUES (?,?,?,?,?,?)",
                      (timeline_id, project_id, version, encoded.decode("utf-8"), _sha(encoded), now))
            c.execute("UPDATE workflow_stages SET status='completed',updated_at=? WHERE project_id=? AND stage_key='timeline'", (now, project_id))
        return {"id": timeline_id, "project_id": project_id, "version": version, "timeline": timeline,
                "sha256": _sha(encoded), "relative_path": relative, "created_at": now}

    def get_timeline(self, project_id: str, version: int | None = None) -> dict | None:
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM timeline_versions WHERE project_id=? AND version=?", (project_id, version)).fetchone() if version else c.execute(
                "SELECT * FROM timeline_versions WHERE project_id=? ORDER BY version DESC LIMIT 1", (project_id,)).fetchone()
        if not row:
            return None
        result = dict(row); result["timeline"] = json.loads(result.pop("timeline_json")); result["relative_path"] = f"timelines/v{result['version']}.json"
        return result

    def _asset_version_path(self, c, project_id: str, version_id: str) -> Path:
        row = c.execute("""SELECT av.relative_path,a.project_id FROM asset_versions av JOIN assets a ON a.id=av.asset_id
                           WHERE av.id=?""", (version_id,)).fetchone()
        if not row or row["project_id"] != project_id:
            raise ValueError("timeline references unavailable asset version")
        return self._resolve_relative(project_id, row["relative_path"])

    def _draft_package(self, c, project_id: str, job_id: str,
                       timeline: dict) -> tuple[Path, str, str | None]:
        """Write the portable draft package.

        Returns ``(path, relative_path, note)``. ``note`` is ``None`` for a clean package, or a
        user-facing sentence when files had to be skipped — callers must surface it, so a package
        is never reported as a clean success while quietly omitting data.

        Two different failures, deliberately treated differently:

        * a **missing** file is skipped and recorded in ``manifest.skipped_files`` (storage cleanup
          can legitimately remove assets; the note makes it visible in the export warning);
        * a file whose bytes no longer match the recorded **SHA-256** raises, because that means
          corruption or tampering — shipping it would invalidate the integrity guarantee this
          export is built on.
        """
        relative = f"exports/{job_id}/draft-package.zip"; output = self._resolve_relative(project_id, relative)
        output.parent.mkdir(parents=True, exist_ok=True)
        skipped_files: list[dict] = []
        manifest = {"format": "vmaker-draft-package/v1", "project_id": project_id, "export_job_id": job_id,
                    "notice": "Draft package only. No MP4 was produced because ffmpeg was unavailable or failed.", "files": []}
        rows = c.execute("""SELECT a.id,a.kind,a.source_shot_id,av.id version_id,av.relative_path,av.sha256,av.mime_type
                            FROM assets a JOIN asset_versions av ON av.asset_id=a.id
                            WHERE a.project_id=? AND av.version=(SELECT MAX(v2.version) FROM asset_versions v2 WHERE v2.asset_id=a.id)
                            ORDER BY a.created_at,a.id""", (project_id,)).fetchall()
        shot_rows = c.execute("""SELECT sh.id,sh.position,sh.duration_sec,sh.action FROM shots sh JOIN scenes sc ON sc.id=sh.scene_id
                                 JOIN episodes e ON e.id=sc.episode_id WHERE e.project_id=? ORDER BY e.version DESC,e.position,sc.position,sh.position""", (project_id,)).fetchall()
        seen_shots = set(); shot_rows = [r for r in shot_rows if not (r["id"] in seen_shots or seen_shots.add(r["id"]))]
        csv_io = io.StringIO(); writer = csv.writer(csv_io); writer.writerow(["shot_id", "order", "duration_sec", "action"])
        for i, row in enumerate(shot_rows, 1): writer.writerow([row["id"], i, row["duration_sec"], row["action"]])
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("timeline.json", json.dumps(timeline, ensure_ascii=False, indent=2))
            archive.writestr("shot-list.csv", csv_io.getvalue().encode("utf-8-sig"))
            for row in rows:
                path = self._resolve_relative(project_id, row["relative_path"])
                if not path.is_file():
                    # A vanished file is recorded and reported, never silently dropped: storage
                    # cleanup can legitimately remove assets, and the package still carries the
                    # timeline, the shot list and every file that is intact.
                    skipped_files.append(
                        {"relative_path": str(row["relative_path"]), "source_shot_id": row["source_shot_id"],
                         "reason": "asset file missing"})
                    continue
                # A checksum mismatch means the file was corrupted or altered. That is an integrity
                # failure, not a missing file: shipping it would quietly invalidate the SHA-256
                # guarantee this export is built on.
                if _sha(path.read_bytes()) != row["sha256"]:
                    raise ValueError(
                        f"素材 {row['relative_path']} 的 SHA-256 校验不通过：文件已损坏或被改动，拒绝按完好素材导出")
                name = f"media/{row['id']}-{row['version_id']}{path.suffix}"
                archive.write(path, name); manifest["files"].append({"path": name, "sha256": row["sha256"], "mime_type": row["mime_type"], "source_shot_id": row["source_shot_id"]})
                if row["kind"] == "subtitle": archive.write(path, "subtitles.srt")
            if skipped_files:
                manifest["skipped_files"] = skipped_files
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        note = None
        if skipped_files:
            note = (f"有 {len(skipped_files)} 个素材文件缺失，已从草稿包中省略"
                    "（清单见 manifest.json 的 skipped_files）")
        return output, relative, note

    @staticmethod
    def _ffmpeg() -> str | None:
        """Locate FFmpeg: explicit setting first, then PATH, then the optional bundled binary.

        ``imageio-ffmpeg`` ships a real ffmpeg inside its wheel, so users who install that extra
        get MP4 export without installing FFmpeg themselves. It stays optional: if the package is
        absent or its binary is missing, this returns ``None`` exactly as before.
        """
        configured = os.getenv("VMAKER_FFMPEG_PATH")
        if configured:
            path = Path(configured).expanduser()
            return str(path) if path.is_file() else None
        found = shutil.which("ffmpeg")
        if found:
            return found
        try:
            import imageio_ffmpeg  # 可选依赖；缺失时保持原有行为
        except Exception:
            return None
        try:
            path = Path(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception:
            return None
        return str(path) if path.is_file() else None

    def _render_mp4(self, c, project_id: str, job_id: str, timeline: dict,
                    ffmpeg: str) -> tuple[Path, str, str | None]:
        export_dir = self._resolve_relative(project_id, f"exports/{job_id}"); export_dir.mkdir(parents=True, exist_ok=True)
        # 画布必须跟随项目宽高比：以前这里硬编码 1280x720，竖屏项目会拿到一个横屏成片。
        # 与剪映草稿共用同一套映射，保证两条导出路径的成片尺寸一致。
        project = c.execute("SELECT aspect_ratio FROM projects WHERE id=?", (project_id,)).fetchone()
        canvas, canvas_warning = canvas_for_aspect_ratio(project["aspect_ratio"] if project else None)
        width, height = canvas
        prepared, unsupported = [], []
        for index, clip in enumerate(timeline["tracks"]["video_audio"], 1):
            image = self._asset_version_path(c, project_id, clip["image"]["asset_version_id"])
            audio = self._asset_version_path(c, project_id, clip["audio"]["asset_version_id"])
            prepared.append((index, clip, image, audio))
            # FFmpeg 没有 SVG 解码器，demo 图像 provider 的占位素材正是 SVG。必须在调用之前就拦住：
            # 否则用户只会拿到一句 "CalledProcessError"，完全看不出问题出在素材格式上。
            if image.suffix.lower() not in RASTER_IMAGE_SUFFIXES:
                unsupported.append((clip.get("position") or index, image.suffix.lower() or "无扩展名"))
        if not prepared:
            raise ValueError("时间线上没有可合成的镜头，无法生成 MP4")
        if unsupported:
            detail = "；".join(f"镜头 {position}（{suffix}）" for position, suffix in unsupported[:8])
            extra = f" 等共 {len(unsupported)} 个镜头" if len(unsupported) > 8 else ""
            raise ValueError(
                f"有 {len(unsupported)} 个镜头的素材不是位图，无法合成 MP4（FFmpeg 不支持 SVG 等矢量格式）：{detail}{extra}。"
                "请先在「分镜工作台」为这些镜头生成画面，或把图像能力配置为真实服务商后重新生成"
            )
        clips = []
        for index, clip, image, audio in prepared:
            output = export_dir / f"clip-{index:04}.mp4"
            argv = [ffmpeg, "-y", "-loop", "1", "-i", str(image), "-i", str(audio), "-t", str(clip["duration_sec"]),
                    "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                           f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(output)]
            completed = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
            if completed.returncode != 0:
                raise ValueError(f"FFmpeg 合成第 {index} 个镜头失败（退出码 {completed.returncode}）：{_stderr_tail(completed.stderr)}")
            clips.append(output)
        concat = export_dir / "concat.txt"
        concat.write_text("".join(f"file '{p.name}'\n" for p in clips), encoding="utf-8")
        output = export_dir / "video.mp4"
        completed = subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "1", "-i", str(concat), "-c", "copy", str(output)],
                                   cwd=export_dir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
        if completed.returncode != 0:
            raise ValueError(f"FFmpeg 拼接失败（退出码 {completed.returncode}）：{_stderr_tail(completed.stderr)}")
        # 组装成功不等于文件可用：这里必须真正回读一遍，否则会把损坏的成片当成功交付。
        verify_mp4_output(ffmpeg, output,
                          expected_duration_sec=float(timeline.get("duration_sec") or 0),
                          expected_canvas=(width, height))
        return output, f"exports/{job_id}/video.mp4", canvas_warning

    def _jianying_draft(self, c, project_id: str, job_id: str,
                        timeline: dict) -> tuple[Path, str, str, bool]:
        """Build a real Jianying draft, falling back to the generic draft package on any failure.

        Returns ``(path, relative_path, warning, suspect)``. ``suspect`` is true when the user has
        something to look at: skipped shots or a caveat worth reading, so the export is reported as
        ``completed_with_warnings`` rather than a clean success. The path is always something that
        can be stored as an artifact: a library that is missing, incompatible, or that throws while
        building means "no Jianying draft", never a failed export.
        """
        if not jianying_library_available():
            path, relative, package_note = self._draft_package(c, project_id, job_id, timeline)
            version = jianying_library_version()
            detail = f"未安装或版本不兼容（{JIANYING_LIBRARY_NAME} {version or '未知版本'}）"
            return path, relative, _with_note(f"{JIANYING_LIBRARY_NAME} 不可用：{detail}；已输出通用草稿包", package_note), True
        try:
            drafts_dir = get_settings().jianying_drafts_dir
            result = build_jianying_draft(self.database, self.projects_root, project_id, job_id,
                                         timeline, drafts_dir=drafts_dir)
            if sum(result.segments.values()) == 0:
                # 一份没有任何片段的草稿不是可用产物——交付它等于留下半成品。这里改回退成通用草稿包，
                # 并把「为什么一个片段都没有」写成可直接阅读的原因。
                path, relative, package_note = self._draft_package(c, project_id, job_id, timeline)
                reasons = "；".join(
                    f"镜头{item.get('shot_sequence')}（{item.get('reason')}）" for item in result.skipped[:8]
                ) or "时间线上没有可用素材"
                return path, relative, _with_note(
                    f"生成的剪映草稿没有任何片段，未作为草稿交付，已改为输出通用草稿包：{reasons}",
                    package_note), True
        except Exception as exc:
            path, relative, package_note = self._draft_package(c, project_id, job_id, timeline)
            return path, relative, _with_note(
                f"{JIANYING_LIBRARY_NAME} 构建失败：{type(exc).__name__}: {exc}；已输出通用草稿包",
                package_note), True
        relative = Path(result.zip_path).resolve().relative_to(self._project_dir(project_id)).as_posix()
        warning = result.summary()
        if result.warnings:
            warning = f"{warning}；{'；'.join(result.warnings)}"
        return Path(result.zip_path), relative, warning, bool(result.warnings or result.skipped)

    def create_export(self, project_id: str, timeline_version: int | None = None,
                      format: str = "auto") -> dict | None:  # noqa: A002 - matches the stored column
        """Export a project timeline.

        ``format`` decides what is produced:

        * ``auto`` — MP4 when ffmpeg is usable; otherwise a real Jianying draft when the library is
          available; otherwise the portable draft package.
        * ``mp4`` — MP4 only. Without ffmpeg this keeps the historical behaviour: the honest draft
          package plus a warning, never a silent switch to another format.
        * ``portable_draft`` — the generic, self-contained zip, always.
        * ``jianying_draft`` — a real Jianying draft; falls back to the portable package (with the
          reason in ``warning``) when the library is missing or throws.
        """
        requested = str(format or "auto").strip().lower()
        if requested not in EXPORT_FORMATS:
            raise ValueError(f"unsupported export format: {requested}")
        timeline_record = self.get_timeline(project_id, timeline_version)
        if timeline_record is None:
            if not self.ensure_project(project_id): return None
            raise ValueError("timeline required")
        job_id, now = _id("export"), utc_now()
        with self.database.connect() as c:
            # 先记下用户**请求**的格式：成功时会被改写成实际交付的产物，失败时不该谎称请求过 auto。
            c.execute("INSERT INTO export_jobs (id,project_id,timeline_version_id,status,format,created_at,started_at) VALUES (?,?,?,'running',?,?,?)",
                      (job_id, project_id, timeline_record["id"], requested, now, now))
            warning = None
            try:
                ffmpeg = self._ffmpeg()
                if requested == "jianying_draft":
                    path, relative, warning, suspect = self._jianying_draft(c, project_id, job_id, timeline_record["timeline"])
                    if relative.endswith("draft-package.zip"):  # the library was unusable: fell back
                        kind = "draft_package"
                        status_value = "completed_with_warnings"
                    else:
                        kind = "jianying_draft"
                        # A draft with skipped shots, a replaced material, or a caveat to read is
                        # not a clean success: the GUI has to show the warning.
                        status_value = "completed_with_warnings" if suspect else "completed"
                    mime = "application/zip"
                elif requested == "mp4" or (requested == "auto" and bool(ffmpeg)):
                    if ffmpeg:
                        try:
                            path, relative, canvas_note = self._render_mp4(c, project_id, job_id, timeline_record["timeline"], ffmpeg)
                            status_value, kind, mime = "completed", "mp4", "video/mp4"
                            # 宽高比无法识别时画布会回退，这件事必须让用户看到。
                            if canvas_note:
                                status_value, warning = "completed_with_warnings", canvas_note
                        except (subprocess.SubprocessError, OSError, ValueError) as exc:
                            path, relative, package_note = self._draft_package(c, project_id, job_id, timeline_record["timeline"])
                            status_value, kind, mime = "completed_with_warnings", "draft_package", "application/zip"
                            warning = _with_note(
                                f"ffmpeg export failed; produced honest draft package instead: {type(exc).__name__}: {exc}",
                                package_note)
                    else:
                        # ``format='mp4'`` without ffmpeg keeps its historical meaning: no MP4, and
                        # the honest portable package instead of quietly becoming a Jianying draft.
                        path, relative, package_note = self._draft_package(c, project_id, job_id, timeline_record["timeline"])
                        status_value, kind, mime = "completed_with_warnings", "draft_package", "application/zip"
                        warning = _with_note(
                            "ffmpeg unavailable; no MP4 was produced. A portable draft package was created.",
                            package_note)
                elif requested == "auto":
                    # No ffmpeg: a draft the user can actually open beats a generic zip.
                    path, relative, draft_warning, _ = self._jianying_draft(c, project_id, job_id, timeline_record["timeline"])
                    mime = "application/zip"
                    status_value = "completed_with_warnings"
                    if relative.endswith("draft-package.zip"):
                        kind = "draft_package"
                        warning = f"ffmpeg unavailable; no MP4 was produced. {draft_warning}"
                    else:
                        kind = "jianying_draft"
                        warning = f"ffmpeg unavailable; a real Jianying draft was produced instead. {draft_warning}"
                else:  # portable_draft: exactly what the old no-ffmpeg path produced
                    path, relative, package_note = self._draft_package(c, project_id, job_id, timeline_record["timeline"])
                    kind, mime = "draft_package", "application/zip"
                    status_value = "completed_with_warnings"
                    warning = _with_note(
                        ("portable draft package requested; no MP4 was produced. "
                         "A portable draft package was created."
                         if ffmpeg else
                         "ffmpeg unavailable; no MP4 was produced. A portable draft package was created."),
                        package_note)
                data = path.read_bytes(); finished = utc_now(); artifact_id = _id("artifact")
                c.execute("INSERT INTO export_artifacts (id,export_job_id,kind,relative_path,mime_type,sha256,byte_size,created_at) VALUES (?,?,?,?,?,?,?,?)",
                          (artifact_id, job_id, kind, relative, mime, _sha(data), len(data), finished))
                c.execute("UPDATE export_jobs SET status=?,format=?,warning=?,finished_at=? WHERE id=?",
                          (status_value, kind, warning, finished, job_id))
                c.execute("UPDATE workflow_stages SET status='completed',updated_at=? WHERE project_id=? AND stage_key='export'", (finished, project_id))
            except Exception as exc:
                finished = utc_now(); c.execute("UPDATE export_jobs SET status='failed',error=?,finished_at=? WHERE id=?", (str(exc), finished, job_id))
        return self.get_export(project_id, job_id)

    def get_export(self, project_id: str, export_id: str) -> dict | None:
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM export_jobs WHERE id=? AND project_id=?", (export_id, project_id)).fetchone()
            if not row: return None
            artifacts = [dict(v) for v in c.execute("SELECT * FROM export_artifacts WHERE export_job_id=? ORDER BY created_at", (export_id,)).fetchall()]
        result = dict(row); result["artifacts"] = artifacts
        return result

    def list_exports(self, project_id: str, limit: int = 20) -> list[dict]:
        """Recent export jobs, newest first, artifacts included.

        Without this, a failed export left no trace in the UI at all: the page always opened on a
        clean form, there was no history route, and the failure was only visible in the database.
        A failure the user cannot look up again is not much better than no message at all.
        """
        limit = max(1, min(int(limit), 100))
        with self.database.connect() as c:
            rows = c.execute(
                "SELECT * FROM export_jobs WHERE project_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (project_id, limit)).fetchall()
            jobs = []
            for row in rows:
                job = dict(row)
                job["artifacts"] = [dict(v) for v in c.execute(
                    "SELECT * FROM export_artifacts WHERE export_job_id=? ORDER BY created_at", (row["id"],)).fetchall()]
                jobs.append(job)
        return jobs

    def artifact_path(self, project_id: str, artifact_id: str) -> tuple[Path, str, str] | None:
        """Resolve only a database-registered artifact belonging to the requested project."""
        with self.database.connect() as c:
            row = c.execute("""SELECT a.relative_path,a.mime_type,a.kind FROM export_artifacts a
                JOIN export_jobs j ON j.id=a.export_job_id JOIN projects p ON p.id=j.project_id
                WHERE a.id=? AND j.project_id=? AND p.deleted_at IS NULL""", (artifact_id, project_id)).fetchone()
        if not row: return None
        try: path = self._resolve_relative(project_id, str(row["relative_path"]))
        except ValueError: return None
        if not path.is_file(): return None
        return path, str(row["mime_type"]), path.name
