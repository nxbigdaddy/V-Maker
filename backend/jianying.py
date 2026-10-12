"""Real JianyingPro (剪映) draft generation through the optional ``pyJianYingDraft`` library.

Scope and honest limits — read this before trusting a generated draft:

* **Draft generation only.** The upstream library's README states that draft *generation* supports
  current Jianying versions, but its automatic *export* (which drives the Jianying GUI) only works
  with Jianying 6 and below. This module therefore never tries to render or export anything.
* **Nothing here has been verified against a real Jianying installation.** This project has no
  Jianying available, so "the draft opens", and whether the empty ``draft_name`` /
  ``draft_fold_path`` inside ``draft_meta_info.json`` (copied verbatim from the library's own
  template) matters, are both **unverified**. Do not claim otherwise in UI copy or docs.
* **Media is referenced by absolute path.** Jianying stores the absolute file path of every
  material, so a draft stays usable only on this machine and only while those asset files exist.
  Deleting the project's assets breaks an already-generated draft.
* **Jianying cannot import SVG.** The demo image provider produces SVG, so those shots are skipped
  and reported instead of being silently packed into a draft that would show nothing.
* **The library is optional.** Every import happens inside a function, so a missing or broken
  ``pyJianYingDraft`` never breaks importing this module or the export pipeline: the caller falls
  back to the generic draft package and records the reason.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import mimetypes
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from backend.database import Database

LIBRARY_NAME = "pyJianYingDraft"

#: One second in the library's time unit (microseconds).
SEC = 1_000_000

#: A shot whose ``duration_sec`` is unusable gets this long, and the draft says so in ``warnings``.
DEFAULT_SHOT_DURATION_SEC = 3.0

#: Canvas per project aspect ratio. Anything unknown falls back to 16:9 with a warning.
CANVAS_BY_ASPECT_RATIO: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}

DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_FPS = 30

#: Raster formats Jianying can import as a still image. SVG is deliberately absent.
BITMAP_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp"})
BITMAP_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})

_UNSAFE_DRAFT_NAME = re.compile(r'[\\/:*?"<>|]')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

MAX_DRAFT_NAME_CHARS = 60
MAX_TEXT_CHARS = 200
#: How many ``-2``, ``-3`` … variants are tried before falling back to a timestamp suffix.
MAX_NAME_ATTEMPTS = 20
#: A skip reason travels into ``export_jobs.warning`` and the GUI, so it stays short.
MAX_SKIP_REASON_CHARS = 120


class JianyingLibraryUnavailable(RuntimeError):
    """Raised when ``pyJianYingDraft`` cannot be imported or does not expose the needed API."""


@dataclass(frozen=True, slots=True)
class DraftBuildResult:
    """What one draft build really produced, including everything it could not include.

    ``skipped`` is a list of ``{"shot_sequence": int, "reason": str}`` and ``warnings`` is a list
    of human-readable strings: both are surfaced to the user instead of being swallowed.
    """

    draft_name: str
    draft_dir: str
    zip_path: str
    canvas: tuple[int, int]
    fps: int
    segments: dict[str, int] = field(default_factory=dict)
    used_assets: list[str] = field(default_factory=list)
    skipped: list[dict[str, object]] = field(default_factory=list)
    library_version: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: The draft root the caller asked for (the user's Jianying draft directory, when configured).
    requested_draft_dir: str | None = None
    #: The folder actually written inside ``requested_draft_dir``; it may carry a ``-3`` suffix when
    #: a draft of that name already exists there, because an existing draft is never overwritten.
    user_draft_dir: str | None = None
    #: The folder name inside the zip; every entry is stored below ``<name>/``.
    zip_member_prefix: str = ""
    #: True when ``requested_draft_dir`` could not be written (a warning, never a failure).
    user_dir_failed: bool = False

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    def as_dict(self) -> dict[str, object]:
        """A JSON-serializable view, used for the zip's info file and for callers that log it."""
        return {
            "draft_name": self.draft_name,
            "draft_dir": self.draft_dir,
            "zip_path": self.zip_path,
            "zip_member_prefix": self.zip_member_prefix,
            "requested_draft_dir": self.requested_draft_dir,
            "user_draft_dir": self.user_draft_dir,
            "user_dir_failed": self.user_dir_failed,
            "canvas": {"width": self.canvas[0], "height": self.canvas[1]},
            "fps": self.fps,
            "segments": dict(self.segments),
            "used_assets": list(self.used_assets),
            "skipped": list(self.skipped),
            "skipped_count": self.skipped_count,
            "library": LIBRARY_NAME,
            "library_version": self.library_version,
            "warnings": list(self.warnings),
        }

    def summary(self) -> str:
        """One line for ``export_jobs.warning``: what was included, what was skipped, and why."""
        tracks = ", ".join(f"{key}={value}" for key, value in sorted(self.segments.items()))
        parts = [
            f"剪映草稿：{self.draft_name}",
            f"画布 {self.canvas[0]}x{self.canvas[1]}@{self.fps}fps",
            f"轨道片段 {tracks}",
            f"引用素材 {len(self.used_assets)} 个（绝对路径）",
        ]
        if self.skipped:
            reasons = "；".join(
                f"镜头{item.get('shot_sequence')}（{item.get('reason')}）" for item in self.skipped
            )
            parts.append(f"跳过 {len(self.skipped)} 个镜头：{reasons}")
        else:
            parts.append("没有镜头被跳过")
        if self.library_version:
            parts.append(f"{LIBRARY_NAME} {self.library_version}")
        return "；".join(parts)


# --- library probing -------------------------------------------------------------------------


def library_version() -> str | None:
    """The installed ``pyJianYingDraft`` version, or ``None`` when it is not installed.

    Read through ``importlib.metadata`` (with a fallback to the module's own attributes) so a
    missing distribution is reported instead of raising.
    """
    try:
        return importlib.metadata.version(LIBRARY_NAME)
    except importlib.metadata.PackageNotFoundError:
        pass
    except Exception:  # pragma: no cover - a broken metadata directory must not break exports
        pass
    try:
        module = importlib.import_module(LIBRARY_NAME)
    except Exception:
        return None
    for attribute in ("__version__", "VERSION", "version"):
        value = getattr(module, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def library_available() -> bool:
    """True only when the library imports *and* exposes the API a draft build needs."""
    try:
        _load_library()
    except Exception:
        return False
    return True


class _Library:
    """The handful of library objects used here, resolved at call time rather than at import."""

    __slots__ = ("module", "DraftFolder", "TrackSpec", "TrackType", "Timerange",
                 "VideoMaterial", "VideoSegment", "AudioMaterial", "AudioSegment",
                 "TextSegment", "TextStyle")

    def __init__(self, module: object) -> None:
        self.module = module
        self.DraftFolder = getattr(module, "DraftFolder")
        self.TrackSpec = getattr(module, "TrackSpec")
        self.TrackType = getattr(module, "TrackType")
        self.Timerange = getattr(module, "Timerange")
        self.VideoMaterial = getattr(module, "VideoMaterial")
        self.VideoSegment = getattr(module, "VideoSegment")
        self.AudioMaterial = getattr(module, "AudioMaterial")
        self.AudioSegment = getattr(module, "AudioSegment")
        self.TextSegment = getattr(module, "TextSegment")
        self.TextStyle = getattr(module, "TextStyle")


def _load_library() -> _Library:
    """Import the library lazily and verify the public API this module depends on."""
    try:
        module = importlib.import_module(LIBRARY_NAME)
    except Exception as exc:  # ImportError, but also a broken dependency of the library itself
        raise JianyingLibraryUnavailable(f"无法导入 {LIBRARY_NAME}: {type(exc).__name__}: {exc}") from exc
    try:
        library = _Library(module)
    except AttributeError as exc:
        raise JianyingLibraryUnavailable(
            f"{LIBRARY_NAME} 版本不兼容，缺少所需接口: {exc}"
        ) from exc
    for name in ("video", "audio", "text"):
        if not hasattr(library.TrackType, name):
            raise JianyingLibraryUnavailable(f"{LIBRARY_NAME} 的 TrackType 缺少 {name} 轨道")
    return library


# --- small helpers ---------------------------------------------------------------------------


def sanitize_draft_name(name: str) -> str:
    """A Windows-safe draft folder name: no ``\\/:*?"<>|`` runes, no control chars, length-capped."""
    cleaned = _UNSAFE_DRAFT_NAME.sub("-", str(name or ""))
    cleaned = _CONTROL_CHARS.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    if len(cleaned) > MAX_DRAFT_NAME_CHARS:
        cleaned = cleaned[:MAX_DRAFT_NAME_CHARS].rstrip(" .-")
    return cleaned or "V-Maker 草稿"


def canvas_for_aspect_ratio(aspect_ratio: str | None) -> tuple[tuple[int, int], str | None]:
    """Map a project aspect ratio to a canvas, returning a warning for an unrecognized one."""
    key = str(aspect_ratio or "").strip()
    if key in CANVAS_BY_ASPECT_RATIO:
        return CANVAS_BY_ASPECT_RATIO[key], None
    fallback = CANVAS_BY_ASPECT_RATIO[DEFAULT_ASPECT_RATIO]
    return fallback, (
        f"项目宽高比 {key or '未设置'} 未知，画布回退为 {DEFAULT_ASPECT_RATIO} "
        f"{fallback[0]}x{fallback[1]}"
    )


def _classify_asset(mime_type: object, relative_path: object) -> str:
    """``video`` | ``bitmap`` | ``svg`` | ``audio`` | ``other``, from the mime type and suffix."""
    mime = str(mime_type or "").strip().lower().split(";")[0]
    suffix = Path(str(relative_path or "")).suffix.lower()
    if mime.startswith("video/") or suffix in VIDEO_SUFFIXES:
        return "video"
    if mime == "image/svg+xml" or suffix == ".svg":
        return "svg"
    if mime in BITMAP_MIME_TYPES or suffix in BITMAP_SUFFIXES:
        return "bitmap"
    if mime.startswith("audio/"):
        return "audio"
    if not mime and suffix:
        # A provider that stored no mime type at all: fall back to the extension, then to mimetypes.
        guess = (mimetypes.guess_type(f"x{suffix}")[0] or "").lower()
        if guess.startswith("video/"):
            return "video"
        if guess.startswith("audio/"):
            return "audio"
        if guess in BITMAP_MIME_TYPES:
            return "bitmap"
    return "other"


def _text_of(value: object) -> str:
    return " ".join(str(value or "").split())


def _shot_duration_sec(value: object) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return 0.0
    return duration if duration > 0 else 0.0


def _existing_draft_names(folder: Path) -> set[str]:
    try:
        return {entry.name for entry in folder.iterdir() if entry.is_dir()}
    except OSError:
        return set()


def _available_draft_name(root: Path, name: str, reserved: set[str]) -> str:
    """The first free draft folder name: ``name``, then ``name-3``, ``name-4`` … then a timestamp.

    This only *picks* a name; the caller still creates the folder with
    ``allow_replace=False`` so a race against another writer can never silently replace a draft.
    """
    base = sanitize_draft_name(name)
    for attempt in range(2, 2 + MAX_NAME_ATTEMPTS):
        candidate = base if attempt == 2 else f"{base}-{attempt}"
        if candidate not in reserved and not (root / candidate).exists():
            return candidate
    return f"{base}-{time.strftime('%Y%m%d-%H%M%S')}"


def _create_draft(folder, library: _Library, name: str, canvas: tuple[int, int], fps: int,
                  reserved: set[str]) -> tuple[str, object]:
    """Create one draft without ever overwriting a draft the user already has.

    ``allow_replace=False`` makes the library raise ``FileExistsError`` rather than delete an
    existing folder, and the free-name search covers both the live directory listing and the names
    this build already used. A collision appends ``-3``, ``-4`` … and finally a timestamp;
    renaming always happens *before* anything is written, so the folder on disk and the reported
    ``draft_name`` can never disagree.
    """
    root = Path(str(getattr(folder, "folder_path", "")))
    while True:
        candidate = _available_draft_name(root, name, reserved)
        try:
            script = folder.create_draft(candidate, canvas[0], canvas[1], fps=fps, allow_replace=False)
        except FileExistsError:
            reserved.add(candidate)
            continue
        reserved.add(candidate)
        return candidate, script


# --- data loading ----------------------------------------------------------------------------


def _project_info(database: Database, project_id: str) -> dict[str, object] | None:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT id,name,aspect_ratio,deleted_at FROM projects WHERE id=?", (project_id,)
        ).fetchone()
    return dict(row) if row else None


def _ordered_shots(database: Database, project_id: str) -> list[dict[str, object]]:
    """Every shot of the latest episode version, in edit order (episode → scene → position)."""
    with database.connect() as connection:
        rows = connection.execute(
            """SELECT sh.id,sh.position,sh.duration_sec,sh.action,sh.narration_dialogue,sh.scene_id
               FROM shots sh JOIN scenes sc ON sc.id=sh.scene_id JOIN episodes e ON e.id=sc.episode_id
               WHERE e.project_id=? AND e.version=(SELECT MAX(version) FROM episodes WHERE project_id=?)
               ORDER BY e.position,sc.position,sh.position""",
            (project_id, project_id),
        ).fetchall()
    shots: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        shot_id = str(row["id"])
        if shot_id in seen:  # two episode versions could both match; keep the first (newest) one
            continue
        seen.add(shot_id)
        shots.append(dict(row))
    return shots


def _shot_asset_rows(database: Database, project_id: str,
                     shot_ids: list[str]) -> dict[str, list[dict[str, object]]]:
    """Latest version of every shot-scoped asset, newest asset first within each shot."""
    if not shot_ids:
        return {}
    placeholders = ",".join("?" for _ in shot_ids)
    with database.connect() as connection:
        rows = connection.execute(
            f"""SELECT a.id asset_id,a.kind,a.source_shot_id shot_id,a.created_at,
                       av.id version_id,av.version,av.relative_path,av.mime_type,av.byte_size
                FROM assets a JOIN asset_versions av ON av.asset_id=a.id
                WHERE a.project_id=? AND a.source_shot_id IN ({placeholders})
                  AND av.version=(SELECT MAX(v2.version) FROM asset_versions v2 WHERE v2.asset_id=a.id)
                ORDER BY a.created_at DESC,av.version DESC""",
            (project_id, *shot_ids),
        ).fetchall()
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        shot_id = row["shot_id"]
        if shot_id is not None:
            grouped.setdefault(str(shot_id), []).append(dict(row))
    return grouped


def _pick_image_assets(candidates: list[dict[str, object]]) -> list[tuple[dict[str, object], str]]:
    """Every usable still/clip for one shot, best first, each tagged with why it was chosen.

    Order: the newest video asset, then every bitmap, newest first. The *caller* walks this list
    because the library can still refuse a file it cannot decode — a corrupt MP4 must fall back to
    the shot's PNG rather than lose the shot entirely. A shot whose only image is an SVG returns an
    empty list, which is the signal for the "Jianying cannot import SVG" skip reason.
    """
    ranked: list[tuple[dict[str, object], str]] = []
    for item in candidates:
        if item["kind"] == "video" and _classify_asset(item["mime_type"], item["relative_path"]) == "video":
            ranked.append((item, "video"))
    for item in candidates:
        if _classify_asset(item["mime_type"], item["relative_path"]) == "bitmap":
            ranked.append((item, "bitmap"))
    return ranked


def _only_svg(candidates: list[dict[str, object]]) -> bool:
    """True when an SVG is the reason this shot has no usable image."""
    return any(_classify_asset(item["mime_type"], item["relative_path"]) == "svg" for item in candidates)


def _pick_audio_asset(candidates: list[dict[str, object]]) -> dict[str, object] | None:
    """The newest audio asset of one shot (``kind='audio'`` or an audio mime type)."""
    for item in candidates:
        if item["kind"] == "audio" or _classify_asset(item["mime_type"], item["relative_path"]) == "audio":
            return item
    return None


def _media_path(database: Database, project_id: str, asset: dict[str, object]) -> Path | None:
    """Resolve one asset version to an absolute existing file, or ``None`` when it is gone.

    ``MediaRepository._resolve_relative`` is the single authority on path safety, so it is reused
    here (imported lazily to keep this module free of a media-layer import at module load).
    """
    from backend.media import MediaRepository

    try:
        path = MediaRepository(database)._resolve_relative(project_id, str(asset["relative_path"]))
    except ValueError:
        return None
    return path if path.is_file() else None


# --- the draft builder -----------------------------------------------------------------------


@dataclass(slots=True)
class _Outcome:
    """Everything one pass over the shots included, skipped, or had to warn about."""

    video_segments: int = 0
    audio_segments: int = 0
    text_segments: int = 0
    used_assets: list[str] = field(default_factory=list)
    skipped: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    defaulted_shots: list[int] = field(default_factory=list)

    def skip(self, sequence: int, reason: str) -> None:
        self.skipped.append({"shot_sequence": int(sequence), "reason": reason[:MAX_SKIP_REASON_CHARS]})

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def segments(self) -> dict[str, int]:
        return {"video": self.video_segments, "audio": self.audio_segments, "text": self.text_segments}


def _populate(database: Database, project_id: str, script, library: _Library,
              shots: list[dict[str, object]], assets_by_shot: dict[str, list[dict[str, object]]],
              outcome: _Outcome) -> None:
    """Place every usable shot on the video track, back to back, with its audio and its narration.

    The video track is the timeline: each placed segment starts exactly where the previous one
    ended, so a skipped shot leaves no gap and the total duration is the sum of the placed shots.
    """
    video_track = script.append_track(library.TrackSpec(library.TrackType.video, name="画面"))
    audio_track = script.append_track(library.TrackSpec(library.TrackType.audio, name="语音"))
    text_track = script.append_track(library.TrackSpec(library.TrackType.text, name="旁白"))
    style = library.TextStyle(size=6.0, align=1)

    cursor = 0  # microseconds
    for sequence, shot in enumerate(shots, 1):
        duration_sec = _shot_duration_sec(shot.get("duration_sec"))
        if duration_sec <= 0:
            duration_sec = DEFAULT_SHOT_DURATION_SEC
            outcome.defaulted_shots.append(sequence)
        duration_us = max(1, int(round(duration_sec * SEC)))
        candidates = assets_by_shot.get(str(shot["id"]), [])

        ranked = _pick_image_assets(candidates)
        if not ranked:
            outcome.skip(sequence, "该镜头只有 SVG 素材，剪映不支持 SVG" if _only_svg(candidates)
                         else "该镜头没有可用素材（既没有视频，也没有 PNG/JPG/WEBP 位图）")
            continue

        chosen, _ = _place_image(database, project_id, ranked, library, script, video_track,
                                 cursor, duration_us, outcome, sequence)
        if chosen is None:
            continue
        image_path, _ = chosen
        outcome.video_segments += 1
        outcome.used_assets.append(str(image_path))

        # The shot's speech starts exactly where its video does: no drift, no offset.
        audio = _pick_audio_asset(candidates)
        audio_path = _media_path(database, project_id, audio) if audio is not None else None
        if audio is not None and audio_path is None:
            outcome.warn(f"镜头 {sequence} 的语音素材文件缺失，已跳过该段音频")
        if audio_path is not None:
            try:
                script.add_segment(library.AudioSegment(library.AudioMaterial(str(audio_path)),
                                                        library.Timerange(cursor, duration_us)), audio_track)
                outcome.audio_segments += 1
                outcome.used_assets.append(str(audio_path))
            except Exception as exc:
                outcome.warn(f"镜头 {sequence} 的语音片段被剪映库拒绝：{type(exc).__name__}: {exc}")

        text = _text_of(shot.get("narration_dialogue")) or _text_of(shot.get("action"))
        if text:
            try:
                script.add_segment(
                    library.TextSegment(text[:MAX_TEXT_CHARS], library.Timerange(cursor, duration_us),
                                        style=style),
                    text_track)
                outcome.text_segments += 1
            except Exception as exc:
                outcome.warn(f"镜头 {sequence} 的字幕片段被剪映库拒绝：{type(exc).__name__}: {exc}")

        cursor += duration_us


def _place_image(database: Database, project_id: str,
                 ranked: list[tuple[dict[str, object], str]], library: _Library, script,
                 video_track, start_us: int, duration_us: int, outcome: _Outcome,
                 sequence: int) -> tuple[tuple[Path, str] | None, str]:
    """Put one image or clip of a shot on the video track, in preference order.

    Returns ``((resolved path, kind), "primary"|"fallback")`` or ``(None, "")`` when nothing could
    be placed. The library refuses a file it cannot decode (a truncated download, an exotic codec),
    and losing the shot over that would be worse than using the shot's bitmap instead.
    """
    first_error: Exception | None = None
    first_path: Path | None = None
    for index, (candidate, kind) in enumerate(ranked):
        path = _media_path(database, project_id, candidate)
        if path is None:
            continue
        try:
            script.add_segment(library.VideoSegment(library.VideoMaterial(str(path)),
                                                    library.Timerange(start_us, duration_us)), video_track)
        except Exception as exc:
            if first_error is None:
                first_error, first_path = exc, path
            continue
        if index > 0:
            outcome.warn(
                f"镜头 {sequence} 的首选素材被剪映库拒绝（{type(first_error).__name__}: {first_error}），"
                "已改用位图素材"
            )
            return (path, kind), "fallback"
        return (path, kind), "primary"
    if first_error is not None:
        outcome.skip(sequence, f"剪映库拒绝了该素材（{type(first_error).__name__}: {first_error}）")
    elif first_path is None:
        outcome.skip(sequence, "素材文件在磁盘上缺失，无法引用")
    else:  # pragma: no cover - every candidate failed for a reason already reported
        outcome.skip(sequence, "该镜头没有可用素材（既没有视频，也没有 PNG/JPG/WEBP 位图）")
    return None, ""


def _write_zip(zip_path: Path, draft_dir: Path, draft_name: str, info: dict[str, object]) -> None:
    """Package the draft folder verbatim, plus an info file that records every honest caveat."""
    notes = {
        "library": f"{LIBRARY_NAME} {info.get('library_version') or 'unknown'}",
        "scope": "仅生成剪映草稿；不做自动导出（pyJianYingDraft 的自动导出只支持剪映 6 及以下）。",
        "unverified": "未在真实剪映中验证：草稿能否被剪映打开、draft_meta_info.json 中 draft_name 为空是否有影响，均未确认。",
        "asset_paths": "素材以绝对路径引用项目目录中的文件；删除这些素材会让草稿失效，草稿只在同一台机器上可用。",
        "svg": "剪映不支持导入 SVG；只有 SVG 素材的镜头已被跳过并记录在 skipped 中。",
        "draft_meta_info": "draft_meta_info.json 由 pyJianYingDraft 内置模板复制而来，其中 draft_name 与 draft_fold_path 为空字符串。",
    }
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(draft_dir.rglob("*")):
            if path.is_file():
                archive.write(path, f"{draft_name}/{path.relative_to(draft_dir).as_posix()}")
        archive.writestr("jianying-draft-info.json",
                         json.dumps({**info, "notes": notes}, ensure_ascii=False, indent=2))


def build_jianying_draft(database: Database, projects_root: Path | str, project_id: str, job_id: str,
                         timeline: dict | None = None, *,
                         drafts_dir: Path | str | None = None,
                         library: _Library | None = None, version: str | None = None) -> DraftBuildResult:
    """Generate a real Jianying draft for one export job and zip it. Raises on a real failure.

    The caller is expected to catch exceptions and fall back to the generic draft package: this
    function never returns a half-built result that pretends to be a draft.

    * Primary output: ``<projects_root>/<project_id>/exports/<job_id>/draft/<草稿名>/``, packaged
      as ``exports/<job_id>/jianying-draft.zip``.
    * When ``drafts_dir`` (``VMAKER_JIANYING_DRAFTS_DIR``) is set, the same draft is written into
      that folder as well — that is the directory Jianying itself scans. A failure there is only a
      warning: the export still succeeds with the copy inside the project.
    """
    del timeline  # reserved for a future timeline-driven layout; the shots drive the timing today
    project = _project_info(database, project_id)
    if project is None:
        raise ValueError("project not found")

    library = library or _load_library()
    version = version if version is not None else library_version()
    export_dir = Path(projects_root).resolve() / project_id / "exports" / job_id
    export_dir.mkdir(parents=True, exist_ok=True)

    canvas, canvas_warning = canvas_for_aspect_ratio(project.get("aspect_ratio"))
    shots = _ordered_shots(database, project_id)
    assets_by_shot = _shot_asset_rows(database, project_id, [str(shot["id"]) for shot in shots])

    outcome = _Outcome()
    if canvas_warning:
        outcome.warn(canvas_warning)
    if not shots:
        outcome.warn("项目没有分镜（shots），生成的草稿只有空轨道")

    draft_root = export_dir / "draft"
    draft_root.mkdir(parents=True, exist_ok=True)
    folder = library.DraftFolder(str(draft_root))
    reserved = _existing_draft_names(draft_root)
    # A second draft for the same project must never overwrite the first one.
    draft_name, script = _create_draft(folder, library, str(project.get("name") or ""),
                                       canvas, DEFAULT_FPS, reserved)
    draft_dir = draft_root / draft_name

    _populate(database, project_id, script, library, shots, assets_by_shot, outcome)
    try:
        script.save()
    except Exception as exc:
        # The library made a draft folder but cannot finalize it: report it, never pretend.
        raise RuntimeError(f"保存剪映草稿失败（{type(exc).__name__}: {exc}）") from exc

    if outcome.defaulted_shots:
        outcome.warn(
            f"有 {len(outcome.defaulted_shots)} 个镜头缺少有效时长，已按 {DEFAULT_SHOT_DURATION_SEC:g} 秒处理"
            f"（镜头序号：{', '.join(str(item) for item in outcome.defaulted_shots)}）"
        )
    if outcome.skipped:
        outcome.warn(f"有 {len(outcome.skipped)} 个镜头因素材问题被跳过，草稿时间线只包含其余镜头")

    result = DraftBuildResult(
        draft_name=draft_name,
        draft_dir=str(draft_dir),
        zip_path=str(export_dir / "jianying-draft.zip"),
        canvas=canvas,
        fps=DEFAULT_FPS,
        segments=outcome.segments(),
        used_assets=list(outcome.used_assets),
        skipped=list(outcome.skipped),
        library_version=version,
        warnings=list(outcome.warnings),
        requested_draft_dir=str(drafts_dir) if drafts_dir else None,
        zip_member_prefix=f"{draft_name}/",
    )
    if drafts_dir is not None:
        result = _mirror_into_user_folder(result, Path(drafts_dir).expanduser())
    # The zip is written last and from the final result, so the info it carries (including the
    # folder the user's own draft root received) describes what was really produced.
    _write_zip(Path(result.zip_path), draft_dir, draft_name, result.as_dict())
    return result


def _mirror_into_user_folder(result: DraftBuildResult, requested: Path) -> DraftBuildResult:
    """Copy the finished draft into the user's real Jianying draft root.

    Unlike the export copy, this is the directory Jianying itself scans. Any problem — a missing
    drive, no permission, an unmounted network path — degrades to one warning: the export still
    succeeds with the draft inside the project directory and in the zip.
    """
    warnings = list(result.warnings)
    user_dir_failed = False
    user_draft_dir: Path | None = None
    try:
        requested.mkdir(parents=True, exist_ok=True)
        # The name search is what protects a draft the user already opened in Jianying: the target
        # folder is only copied into once it is known to be free.
        user_name = _available_draft_name(requested, result.draft_name, set())
        user_draft_dir = requested / user_name
        _copy_draft_files(Path(result.draft_dir), user_draft_dir)
    except Exception as exc:
        user_dir_failed = True
        warnings.append(
            f"无法写入配置的剪映草稿目录 {requested}（{type(exc).__name__}: {exc}）；"
            "草稿已保留在项目导出目录与 zip 中"
        )
    else:
        warnings.append(f"已同时写入剪映草稿根目录：{user_draft_dir}")

    return DraftBuildResult(
        draft_name=result.draft_name,
        draft_dir=result.draft_dir,
        zip_path=result.zip_path,
        canvas=result.canvas,
        fps=result.fps,
        segments=result.segments,
        used_assets=result.used_assets,
        skipped=result.skipped,
        library_version=result.library_version,
        warnings=warnings,
        requested_draft_dir=str(requested),
        user_draft_dir=str(user_draft_dir) if user_draft_dir else None,
        zip_member_prefix=result.zip_member_prefix,
        user_dir_failed=user_dir_failed,
    )


def _copy_draft_files(source: Path, target: Path) -> None:
    """Copy a finished draft folder onto the user's Jianying draft root."""
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
