"""Storage accounting and safe, opt-in cleanup for the local project media tree.

Every media file lives below ``MediaRepository.projects_root`` (by default
``backend/data/projects/<project_id>/``). Two facts make that tree grow forever:

* regenerating a shot writes a *new* asset directory instead of a new version, and
* deleting a project is a soft delete (``projects.deleted_at``), so its files stay.

This module only ever *reports* the footprint, and, when explicitly asked, removes files that
nothing points at. "Nothing points at" is deliberately narrow and strictly defined: a file is a
reference-holder when

* some ``asset_versions.relative_path`` names it,
* some ``export_artifacts.relative_path`` names it,
* it is the ``timelines/v<version>.json`` file of a ``timeline_versions`` row — that file is still
  written by ``create_timeline`` and still advertised as ``relative_path`` by ``get_timeline``, so
  deleting it would leave the API pointing at a missing file, or
* it sits inside the ``exports/<job_id>/`` directory of an export job that is currently *running*,
  whose intermediate files (``clip-*.mp4``, ``concat.txt``) are registered nowhere.

Paths are compared in POSIX form so a Windows backslash never hides a reference, and every
deletion is re-checked against the database and against the projects root immediately before it
happens.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from backend.database import Database
from backend.media import MediaRepository

# A cleanup response never lists more than this many paths, so the payload stays small.
MAX_TARGET_PATHS = 200


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """One file entry found below the projects root."""

    project_id: str
    relative_path: str  # POSIX, relative to the project directory (the form the database stores)
    path: Path  # absolute path as walked; lexical, never resolved
    size: int
    referenced: bool  # named by an asset_versions or export_artifacts row
    deleted_project: bool  # belongs to a soft-deleted project
    unsafe: bool = False  # resolves outside the projects root: reported, never counted or deleted


@dataclass(frozen=True, slots=True)
class StorageScan:
    root: Path
    files: tuple[ScannedFile, ...]
    project_ids: tuple[str, ...]  # every subdirectory of the root, even one without a project row
    projects: dict[str, tuple[str, bool]]  # project_id -> (name, deleted)


def _within(path: Path, root: Path) -> bool:
    """True when an already-resolved path is the root itself or lives below it."""
    return path == root or root in path.parents


def _normalize_relative(relative_path: object) -> str:
    """Normalize a stored relative path so Windows separators cannot hide a reference match."""
    text = str(relative_path if relative_path is not None else "").replace("\\", "/").strip()
    if not text:
        return ""
    normalized = posixpath.normpath(text)
    return "" if normalized in (".", "/") else normalized


def _reference_index(database: Database) -> tuple[dict[str, set[str]], dict[str, tuple[str, bool]], dict[str, set[str]]]:
    """Read every path the database still references, grouped by project.

    Three sources count as a reference:
      * ``asset_versions.relative_path`` — immutable media;
      * ``export_artifacts.relative_path`` — the delivered export file;
      * ``timeline_versions`` — ``create_timeline`` writes ``timelines/v<version>.json`` and
        ``get_timeline`` keeps advertising that path, so it must not be treated as garbage.

    Running export jobs additionally protect their whole directory by prefix: their intermediate
    files (``clip-*.mp4``, ``concat.txt``) are not registered anywhere, and deleting them mid-render
    would break the export that is currently writing them.
    """
    references: dict[str, set[str]] = {}
    protected_prefixes: dict[str, set[str]] = {}
    with database.connect() as connection:
        projects = {
            str(row["id"]): (str(row["name"]), row["deleted_at"] is not None)
            for row in connection.execute("SELECT id,name,deleted_at FROM projects").fetchall()
        }
        asset_rows = connection.execute(
            "SELECT a.project_id project_id, av.relative_path relative_path"
            " FROM asset_versions av JOIN assets a ON a.id=av.asset_id"
        ).fetchall()
        artifact_rows = connection.execute(
            "SELECT j.project_id project_id, ea.relative_path relative_path"
            " FROM export_artifacts ea JOIN export_jobs j ON j.id=ea.export_job_id"
        ).fetchall()
        timeline_rows = connection.execute(
            "SELECT project_id project_id, version version FROM timeline_versions"
        ).fetchall()
        running_rows = connection.execute(
            "SELECT project_id project_id, id id FROM export_jobs WHERE status='running'"
        ).fetchall()
    for row in (*asset_rows, *artifact_rows):
        key = _normalize_relative(row["relative_path"])
        if key:
            references.setdefault(str(row["project_id"]), set()).add(key)
    for row in timeline_rows:
        key = _normalize_relative(f"timelines/v{int(row['version'])}.json")
        references.setdefault(str(row["project_id"]), set()).add(key)
    for row in running_rows:
        protected_prefixes.setdefault(str(row["project_id"]), set()).add(f"exports/{row['id']}/")
    return references, projects, protected_prefixes


def _iter_files(project_dir: Path, root: Path) -> Iterator[tuple[Path, str]]:
    """Yield ``(lexical path, POSIX path relative to the project directory)`` for every file.

    Directory symlinks are followed, but never past the projects root (that also stops a link
    cycle from making the walk unbounded).
    """
    seen = {root}
    stack = [project_dir]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    resolved = entry.resolve()
                    if resolved not in seen and _within(resolved, root):
                        seen.add(resolved)
                        stack.append(entry)
                    continue
                if entry.is_file():
                    yield entry, entry.relative_to(project_dir).as_posix()
            except OSError:
                continue


def _scan(database: Database) -> StorageScan:
    """Walk the projects root once and classify every regular file in it."""
    root = Path(MediaRepository(database).projects_root).resolve()
    references, projects, protected_prefixes = _reference_index(database)
    files: list[ScannedFile] = []
    project_ids: list[str] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        # A missing or unreadable root is an empty tree, never an error.
        return StorageScan(root, (), (), projects)
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            resolved_dir = entry.resolve()
        except OSError:
            continue
        if resolved_dir == root or not _within(resolved_dir, root):
            continue
        project_id = entry.name
        project_ids.append(project_id)
        deleted_project = projects.get(project_id, (project_id, False))[1]
        referenced = references.get(project_id, frozenset())
        protected = protected_prefixes.get(project_id, frozenset())
        for path, relative_path in _iter_files(entry, root):
            try:
                # A file whose link resolves outside the root is recorded, but never measured or
                # followed: it is reported by cleanup as skipped_unsafe and ignored by the summary.
                safe = _within(path.resolve(), root)
                size = path.stat().st_size if safe else 0
            except OSError:
                continue
            is_referenced = relative_path in referenced or any(
                relative_path.startswith(prefix) for prefix in protected)
            files.append(ScannedFile(project_id, relative_path, path, int(size),
                                     is_referenced, deleted_project, not safe))
    return StorageScan(root, tuple(files), tuple(project_ids), projects)


def storage_summary(database: Database) -> dict[str, object]:
    """Return the on-disk footprint of every project directory below the projects root."""
    scan = _scan(database)
    projects: list[dict[str, object]] = []
    for project_id in scan.project_ids:
        known = scan.projects.get(project_id)
        entry: dict[str, object] = {
            "project_id": project_id,
            "name": known[0] if known else project_id,
            "deleted": known[1] if known else False,
            "bytes": 0,
            "file_count": 0,
        }
        if known is None:
            entry["unknown"] = True
        projects.append(entry)
    by_id = {str(entry["project_id"]): entry for entry in projects}

    total_bytes = file_count = orphan_bytes = orphan_files = 0
    deleted_project_bytes = deleted_project_files = 0
    for item in scan.files:
        if item.unsafe:
            # A link that leaves the root is not part of this tree's footprint.
            continue
        entry = by_id[item.project_id]
        entry["bytes"] = int(entry["bytes"]) + item.size
        entry["file_count"] = int(entry["file_count"]) + 1
        total_bytes += item.size
        file_count += 1
        if not item.referenced:
            orphan_bytes += item.size
            orphan_files += 1
        if item.deleted_project:
            deleted_project_bytes += item.size
            deleted_project_files += 1
    projects.sort(key=lambda entry: (-int(entry["bytes"]), str(entry["project_id"])))
    return {
        "data_dir": str(scan.root),
        "total_bytes": total_bytes,
        "file_count": file_count,
        "orphan_bytes": orphan_bytes,
        "orphan_files": orphan_files,
        "deleted_project_bytes": deleted_project_bytes,
        "deleted_project_files": deleted_project_files,
        "projects": projects,
    }


def _is_referenced_now(database: Database, project_id: str, relative_path: str) -> bool:
    """Re-read the database for one candidate path, right before it would be deleted.

    The scan and the delete loop are not atomic, so a file that was an orphan a moment ago may
    already have been claimed by a running task. This deliberately queries again per target.
    """
    key = _normalize_relative(relative_path)
    if not key:
        return True
    with database.connect() as connection:
        asset_rows = connection.execute(
            "SELECT av.relative_path relative_path FROM asset_versions av"
            " JOIN assets a ON a.id=av.asset_id WHERE a.project_id=?", (project_id,)
        ).fetchall()
        artifact_rows = connection.execute(
            "SELECT ea.relative_path relative_path FROM export_artifacts ea"
            " JOIN export_jobs j ON j.id=ea.export_job_id WHERE j.project_id=?", (project_id,)
        ).fetchall()
        timeline_rows = connection.execute(
            "SELECT version FROM timeline_versions WHERE project_id=?", (project_id,)
        ).fetchall()
        running_rows = connection.execute(
            "SELECT id FROM export_jobs WHERE project_id=? AND status='running'", (project_id,)
        ).fetchall()
    if any(_normalize_relative(row["relative_path"]) == key for row in (*asset_rows, *artifact_rows)):
        return True
    # 与 _reference_index 保持同一套「引用」定义，避免扫描与删除之间的判断不一致。
    if any(_normalize_relative(f"timelines/v{int(row['version'])}.json") == key for row in timeline_rows):
        return True
    return any(key.startswith(f"exports/{row['id']}/") for row in running_rows)


def _safe_target(root: Path, project_id: str, relative_path: str) -> Path | None:
    """Mirror ``MediaRepository._resolve_relative``: refuse anything that leaves the root.

    The *lexical* path is returned so the following ``unlink`` removes the directory entry that
    was scanned. ``Path.unlink`` never follows a symlink, so the only thing that can disappear is
    something that already lives inside the projects root.
    """
    if not project_id or project_id in (".", "..") or "/" in project_id or "\\" in project_id:
        return None
    lexical = root / project_id / relative_path
    try:
        resolved = lexical.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not _within(resolved, root):
        return None
    try:
        if not lexical.is_file():
            return None
    except OSError:
        return None
    return lexical


def _prune_empty_parents(path: Path, project_dir: Path, root: Path) -> int:
    """Remove the now-empty directories a deletion left behind, stopping at the project dir."""
    removed = 0
    current = path.parent
    while current != project_dir and current != root and _within(current, root):
        try:
            current.rmdir()  # rmdir only succeeds on an empty directory
        except OSError:
            break
        removed += 1
        current = current.parent
    return removed


def cleanup_storage(database: Database, dry_run: bool = True,
                    include_deleted_projects: bool = False) -> dict[str, object]:
    """Remove files no database row references; a dry run only lists them.

    Safety contract, in order of application:

    1. ``dry_run`` defaults to ``True``; nothing is deleted unless the caller passes ``False``.
    2. A file is a target only when neither ``asset_versions`` nor ``export_artifacts`` names it.
    3. Files of a soft-deleted project become targets only with ``include_deleted_projects=True``;
       a file that is both an orphan and part of a deleted project is still counted exactly once.
    4. Each target is re-checked against the database immediately before deletion.
    5. Only regular files whose resolved path stays inside the projects root are deleted; anything
       that escapes is skipped and reported as ``skipped_unsafe``.
    6. Empty directories left behind are pruned, but never the root itself.
    7. A failing deletion is reported as ``failed`` instead of raising.
    8. At most ``MAX_TARGET_PATHS`` paths are returned, with ``truncated`` set when cut short.
    """
    scan = _scan(database)
    root = scan.root
    candidates: list[ScannedFile] = []
    seen_paths: set[str] = set()
    kept_referenced = 0
    skipped_unsafe = 0
    for item in scan.files:
        if item.unsafe:
            # Rule 5: anything whose resolved path leaves the root is skipped, never deleted.
            skipped_unsafe += 1
            continue
        if item.referenced:
            kept_referenced += 1
            continue
        if item.deleted_project and not include_deleted_projects:
            continue
        token = str(item.path)
        if token in seen_paths:  # never count or delete the same file twice
            continue
        seen_paths.add(token)
        candidates.append(item)

    targets = [item.relative_path for item in candidates[:MAX_TARGET_PATHS]]
    summary: dict[str, object] = {
        "dry_run": dry_run,
        "include_deleted_projects": include_deleted_projects,
        "removed_files": 0,
        "removed_bytes": 0,
        "kept_referenced": kept_referenced,
        "target_files": len(candidates),
        "target_bytes": sum(item.size for item in candidates),
        "skipped_unsafe": skipped_unsafe,
        "targets": targets,
    }
    if len(candidates) > MAX_TARGET_PATHS:
        summary["truncated"] = True

    removed_files = removed_bytes = removed_dirs = failed = 0
    if not dry_run:
        for item in candidates:
            if _is_referenced_now(database, item.project_id, item.relative_path):
                kept_referenced += 1
                continue
            target = _safe_target(root, item.project_id, item.relative_path)
            if target is None:
                skipped_unsafe += 1
                continue
            try:
                size = target.stat().st_size
                target.unlink()
            except OSError:
                failed += 1
                continue
            removed_files += 1
            removed_bytes += size
            removed_dirs += _prune_empty_parents(target, root / item.project_id, root)

    summary.update({
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "removed_dirs": removed_dirs,
        "kept_referenced": kept_referenced,
        "failed": failed,
        "skipped_unsafe": skipped_unsafe,
    })
    return summary
