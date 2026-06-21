"""Hermes-like bundled skill sync into the active Pulsara skills tree."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Iterator, Literal
from uuid import uuid4

from pulsara_agent import __version__
from pulsara_agent.capability.local_skills import (
    BUNDLED_SKILL_PROVENANCE_FILE_NAME,
    PULSARA_HOME_ENV,
    SKILL_FILE_NAME,
)


BUNDLED_MANIFEST_FILE_NAME = ".bundled_manifest"
BUNDLED_OPT_OUT_MARKER_NAME = ".no-bundled-skills"
RESTORE_BACKUPS_DIR_NAME = ".restore-backups"
DEFAULT_BUNDLED_FROM = "pulsara-agent"

BundledSkillSyncAction = Literal[
    "installed",
    "updated",
    "unchanged",
    "skipped_modified",
    "skipped_deleted",
    "skipped_existing_unmanaged",
    "source_removed",
    "opted_out",
    "source_missing",
]
BundledSkillStatusState = Literal[
    "available_to_sync",
    "installed",
    "modified",
    "deleted",
    "unmanaged_collision",
    "source_removed",
]
BundledSkillResetAction = Literal["reset", "restored_deleted", "unchanged", "not_bundled"]


@dataclass(frozen=True, slots=True)
class BundledSkillSyncItem:
    name: str
    action: BundledSkillSyncAction
    message: str
    target_path: Path | None = None
    source_hash: str | None = None
    manifest_hash: str | None = None
    target_hash: str | None = None

    def to_dict(self) -> dict[str, object]:
        return _compact_dict(
            {
                "name": self.name,
                "action": self.action,
                "message": self.message,
                "target_path": str(self.target_path) if self.target_path is not None else None,
                "source_hash": self.source_hash,
                "manifest_hash": self.manifest_hash,
                "target_hash": self.target_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class BundledSkillSyncResult:
    pulsara_home: Path
    skills_root: Path
    source_root: Path | None
    opt_out: bool
    manifest_path: Path
    items: tuple[BundledSkillSyncItem, ...]
    manifest_written: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "pulsara_home": str(self.pulsara_home),
            "skills_root": str(self.skills_root),
            "source_root": str(self.source_root) if self.source_root is not None else None,
            "opt_out": self.opt_out,
            "manifest_path": str(self.manifest_path),
            "manifest_written": self.manifest_written,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class BundledSkillStatus:
    name: str
    state: BundledSkillStatusState
    target_path: Path | None = None
    source_hash: str | None = None
    manifest_hash: str | None = None
    target_hash: str | None = None
    provenance_source: str | None = None

    def to_dict(self) -> dict[str, object]:
        return _compact_dict(
            {
                "name": self.name,
                "state": self.state,
                "target_path": str(self.target_path) if self.target_path is not None else None,
                "source_hash": self.source_hash,
                "manifest_hash": self.manifest_hash,
                "target_hash": self.target_hash,
                "provenance_source": self.provenance_source,
            }
        )


@dataclass(frozen=True, slots=True)
class BundledSkillStatusResult:
    pulsara_home: Path
    skills_root: Path
    source_root: Path | None
    opt_out: bool
    manifest_path: Path
    statuses: tuple[BundledSkillStatus, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "pulsara_home": str(self.pulsara_home),
            "skills_root": str(self.skills_root),
            "source_root": str(self.source_root) if self.source_root is not None else None,
            "opt_out": self.opt_out,
            "manifest_path": str(self.manifest_path),
            "statuses": [status.to_dict() for status in self.statuses],
        }


@dataclass(frozen=True, slots=True)
class BundledSkillResetResult:
    name: str
    action: BundledSkillResetAction
    message: str
    target_path: Path
    backup_path: Path | None = None
    origin_hash: str | None = None
    manifest_written: bool = False

    def to_dict(self) -> dict[str, object]:
        return _compact_dict(
            {
                "name": self.name,
                "action": self.action,
                "message": self.message,
                "target_path": str(self.target_path),
                "backup_path": str(self.backup_path) if self.backup_path is not None else None,
                "origin_hash": self.origin_hash,
                "manifest_written": self.manifest_written,
            }
        )


def default_pulsara_home() -> Path:
    raw = os.getenv(PULSARA_HOME_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home().joinpath(".pulsara").resolve()


def user_product_skills_root(pulsara_home: Path | None = None) -> Path:
    return (pulsara_home or default_pulsara_home()).expanduser().resolve() / "skills"


def sync_bundled_skills(
    *,
    pulsara_home: Path | None = None,
    source_root: Path | None = None,
    override_opt_out: bool = False,
    bundled_from: str = DEFAULT_BUNDLED_FROM,
    bundled_version: str = __version__,
) -> BundledSkillSyncResult:
    home = (pulsara_home or default_pulsara_home()).expanduser().resolve()
    skills_root = user_product_skills_root(home)
    manifest_path = skills_root / BUNDLED_MANIFEST_FILE_NAME
    if _is_opted_out(home) and not override_opt_out:
        return BundledSkillSyncResult(
            pulsara_home=home,
            skills_root=skills_root,
            source_root=None,
            opt_out=True,
            manifest_path=manifest_path,
            items=(
                BundledSkillSyncItem(
                    name="*",
                    action="opted_out",
                    message=f"Bundled skill sync skipped because {BUNDLED_OPT_OUT_MARKER_NAME} exists.",
                ),
            ),
        )

    with _bundled_source_root(source_root) as resolved_source_root:
        if not resolved_source_root.exists() or not resolved_source_root.is_dir():
            return BundledSkillSyncResult(
                pulsara_home=home,
                skills_root=skills_root,
                source_root=resolved_source_root,
                opt_out=False,
                manifest_path=manifest_path,
                items=(
                    BundledSkillSyncItem(
                        name="*",
                        action="source_missing",
                        message=f"Bundled skill source root is missing: {resolved_source_root}",
                    ),
                ),
            )

        skills_root.mkdir(parents=True, exist_ok=True)
        manifest = _read_manifest(manifest_path)
        next_manifest = dict(manifest)
        source_hashes = _source_skill_hashes(resolved_source_root)
        items: list[BundledSkillSyncItem] = []

        for name, origin_hash in sorted(source_hashes.items()):
            source_dir = resolved_source_root / name
            target_dir = skills_root / name
            recorded_hash = manifest.get(name)

            if recorded_hash is None:
                if target_dir.exists():
                    target_hash = compute_skill_dir_hash(target_dir)
                    items.append(
                        BundledSkillSyncItem(
                            name=name,
                            action="skipped_existing_unmanaged",
                            message="Target skill already exists but is not managed as a bundled skill.",
                            target_path=target_dir,
                            source_hash=origin_hash,
                            target_hash=target_hash,
                        )
                    )
                    continue
                _replace_dir_from_source(source_dir, target_dir)
                _write_provenance(
                    target_dir,
                    name=name,
                    origin_hash=origin_hash,
                    bundled_from=bundled_from,
                    bundled_version=bundled_version,
                )
                next_manifest[name] = origin_hash
                items.append(
                    BundledSkillSyncItem(
                        name=name,
                        action="installed",
                        message="Bundled skill installed.",
                        target_path=target_dir,
                        source_hash=origin_hash,
                    )
                )
                continue

            if not target_dir.exists():
                items.append(
                    BundledSkillSyncItem(
                        name=name,
                        action="skipped_deleted",
                        message="Bundled skill was deleted by the user; not restoring during sync.",
                        target_path=target_dir,
                        source_hash=origin_hash,
                        manifest_hash=recorded_hash,
                    )
                )
                continue

            target_hash = compute_skill_dir_hash(target_dir)
            if target_hash != recorded_hash:
                items.append(
                    BundledSkillSyncItem(
                        name=name,
                        action="skipped_modified",
                        message="Bundled skill has user modifications; not overwriting.",
                        target_path=target_dir,
                        source_hash=origin_hash,
                        manifest_hash=recorded_hash,
                        target_hash=target_hash,
                    )
                )
                continue

            if recorded_hash != origin_hash:
                _replace_dir_from_source(source_dir, target_dir)
                _write_provenance(
                    target_dir,
                    name=name,
                    origin_hash=origin_hash,
                    bundled_from=bundled_from,
                    bundled_version=bundled_version,
                )
                next_manifest[name] = origin_hash
                items.append(
                    BundledSkillSyncItem(
                        name=name,
                        action="updated",
                        message="Bundled skill updated from package source.",
                        target_path=target_dir,
                        source_hash=origin_hash,
                        manifest_hash=recorded_hash,
                        target_hash=target_hash,
                    )
                )
                continue

            _ensure_provenance(
                target_dir,
                name=name,
                origin_hash=origin_hash,
                bundled_from=bundled_from,
                bundled_version=bundled_version,
            )
            items.append(
                BundledSkillSyncItem(
                    name=name,
                    action="unchanged",
                    message="Bundled skill already up to date.",
                    target_path=target_dir,
                    source_hash=origin_hash,
                    manifest_hash=recorded_hash,
                    target_hash=target_hash,
                )
            )

        for name, recorded_hash in sorted(manifest.items()):
            if name in source_hashes:
                continue
            next_manifest.pop(name, None)
            items.append(
                BundledSkillSyncItem(
                    name=name,
                    action="source_removed",
                    message="Bundled source no longer exists; manifest entry removed.",
                    target_path=skills_root / name,
                    manifest_hash=recorded_hash,
                )
            )

        manifest_written = next_manifest != manifest
        if manifest_written:
            _write_manifest_atomic(manifest_path, next_manifest)
        return BundledSkillSyncResult(
            pulsara_home=home,
            skills_root=skills_root,
            source_root=resolved_source_root,
            opt_out=False,
            manifest_path=manifest_path,
            items=tuple(items),
            manifest_written=manifest_written,
        )


def bundled_skills_status(
    *,
    pulsara_home: Path | None = None,
    source_root: Path | None = None,
) -> BundledSkillStatusResult:
    home = (pulsara_home or default_pulsara_home()).expanduser().resolve()
    skills_root = user_product_skills_root(home)
    manifest_path = skills_root / BUNDLED_MANIFEST_FILE_NAME
    with _bundled_source_root(source_root) as resolved_source_root:
        source_hashes = (
            _source_skill_hashes(resolved_source_root)
            if resolved_source_root.exists() and resolved_source_root.is_dir()
            else {}
        )
        manifest = _read_manifest(manifest_path)
        statuses: list[BundledSkillStatus] = []
        for name in sorted(set(source_hashes) | set(manifest)):
            source_hash = source_hashes.get(name)
            manifest_hash = manifest.get(name)
            target_dir = skills_root / name
            target_exists = target_dir.exists()
            target_hash = compute_skill_dir_hash(target_dir) if target_exists else None
            provenance = _read_provenance(target_dir) if target_exists else {}
            if source_hash is None:
                state: BundledSkillStatusState = "source_removed"
            elif manifest_hash is None and target_exists:
                state = "unmanaged_collision"
            elif manifest_hash is None:
                state = "available_to_sync"
            elif not target_exists:
                state = "deleted"
            elif target_hash == manifest_hash:
                state = "installed"
            else:
                state = "modified"
            statuses.append(
                BundledSkillStatus(
                    name=name,
                    state=state,
                    target_path=target_dir,
                    source_hash=source_hash,
                    manifest_hash=manifest_hash,
                    target_hash=target_hash,
                    provenance_source=_string_value(provenance.get("source")),
                )
            )
        return BundledSkillStatusResult(
            pulsara_home=home,
            skills_root=skills_root,
            source_root=resolved_source_root,
            opt_out=_is_opted_out(home),
            manifest_path=manifest_path,
            statuses=tuple(statuses),
        )


def reset_bundled_skill(
    name: str,
    *,
    pulsara_home: Path | None = None,
    source_root: Path | None = None,
    bundled_from: str = DEFAULT_BUNDLED_FROM,
    bundled_version: str = __version__,
) -> BundledSkillResetResult:
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"Invalid bundled skill name: {name!r}")
    home = (pulsara_home or default_pulsara_home()).expanduser().resolve()
    skills_root = user_product_skills_root(home)
    target_dir = skills_root / name
    manifest_path = skills_root / BUNDLED_MANIFEST_FILE_NAME
    with _bundled_source_root(source_root) as resolved_source_root:
        source_dir = resolved_source_root / name
        if not source_dir.is_dir() or not (source_dir / SKILL_FILE_NAME).is_file():
            raise ValueError(f"Bundled skill source not found: {name}")
        origin_hash = compute_skill_dir_hash(source_dir)
        manifest = _read_manifest(manifest_path)
        provenance = _read_provenance(target_dir) if target_dir.exists() else {}
        target_is_bundled = manifest.get(name) is not None or provenance.get("source") == "bundled"
        if target_dir.exists() and not target_is_bundled:
            return BundledSkillResetResult(
                name=name,
                action="not_bundled",
                message="Target exists but is not managed as a bundled skill.",
                target_path=target_dir,
            )
        if target_dir.exists() and compute_skill_dir_hash(target_dir) == origin_hash:
            _ensure_provenance(
                target_dir,
                name=name,
                origin_hash=origin_hash,
                bundled_from=bundled_from,
                bundled_version=bundled_version,
            )
            next_manifest = {**manifest, name: origin_hash}
            manifest_written = next_manifest != manifest
            if manifest_written:
                skills_root.mkdir(parents=True, exist_ok=True)
                _write_manifest_atomic(manifest_path, next_manifest)
            return BundledSkillResetResult(
                name=name,
                action="unchanged",
                message="Bundled skill already matches package source.",
                target_path=target_dir,
                origin_hash=origin_hash,
                manifest_written=manifest_written,
            )

        backup_path = None
        if target_dir.exists():
            backup_path = _backup_existing_skill(target_dir, skills_root=skills_root)
        _replace_dir_from_source(source_dir, target_dir)
        _write_provenance(
            target_dir,
            name=name,
            origin_hash=origin_hash,
            bundled_from=bundled_from,
            bundled_version=bundled_version,
        )
        next_manifest = {**manifest, name: origin_hash}
        skills_root.mkdir(parents=True, exist_ok=True)
        _write_manifest_atomic(manifest_path, next_manifest)
        return BundledSkillResetResult(
            name=name,
            action="reset" if backup_path is not None else "restored_deleted",
            message="Bundled skill restored from package source.",
            target_path=target_dir,
            backup_path=backup_path,
            origin_hash=origin_hash,
            manifest_written=True,
        )


def compute_skill_dir_hash(skill_dir: Path) -> str:
    digest = sha256()
    for path in sorted(skill_dir.rglob("*"), key=lambda item: item.relative_to(skill_dir).as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(skill_dir).as_posix()
        if _is_ignored_hash_path(relative):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@contextmanager
def _bundled_source_root(source_root: Path | None = None) -> Iterator[Path]:
    if source_root is not None:
        yield source_root.expanduser().resolve()
        return
    traversable = resources.files("pulsara_agent").joinpath("bundled_skills")
    with resources.as_file(traversable) as path:
        yield path


def _source_skill_hashes(source_root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for child in sorted(source_root.iterdir(), key=lambda path: path.name):
        if child.name.startswith(".") or not child.is_dir():
            continue
        if not (child / SKILL_FILE_NAME).is_file():
            continue
        hashes[child.name] = compute_skill_dir_hash(child)
    return hashes


def _read_manifest(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    manifest: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        name, origin_hash = line.split(":", 1)
        name = name.strip()
        origin_hash = origin_hash.strip()
        if name and origin_hash:
            manifest[name] = origin_hash
    return manifest


def _write_manifest_atomic(path: Path, manifest: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{name}:{origin_hash}\n" for name, origin_hash in sorted(manifest.items())]
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _replace_dir_from_source(source_dir: Path, target_dir: Path) -> None:
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = target_dir.parent / f".{target_dir.name}.tmp-{uuid4().hex}"
    old_dir = target_dir.parent / f".{target_dir.name}.old-{uuid4().hex}"
    shutil.copytree(source_dir, tmp_dir, ignore=_copy_ignore)
    try:
        if target_dir.exists():
            target_dir.rename(old_dir)
        tmp_dir.rename(target_dir)
    except Exception:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if old_dir.exists() and not target_dir.exists():
            old_dir.rename(target_dir)
        raise
    finally:
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)
    _fsync_dir(target_dir.parent)


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in {"__pycache__", ".DS_Store"}}
    ignored.update(name for name in names if name.endswith(".pyc"))
    return ignored


def _write_provenance(
    skill_dir: Path,
    *,
    name: str,
    origin_hash: str,
    bundled_from: str,
    bundled_version: str,
) -> None:
    payload = {
        "source": "bundled",
        "name": name,
        "bundled_from": bundled_from,
        "bundled_version": bundled_version,
        "origin_hash": origin_hash,
    }
    path = skill_dir / BUNDLED_SKILL_PROVENANCE_FILE_NAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _ensure_provenance(
    skill_dir: Path,
    *,
    name: str,
    origin_hash: str,
    bundled_from: str,
    bundled_version: str,
) -> None:
    expected = {
        "source": "bundled",
        "name": name,
        "bundled_from": bundled_from,
        "bundled_version": bundled_version,
        "origin_hash": origin_hash,
    }
    if _read_provenance(skill_dir) == expected:
        return
    _write_provenance(
        skill_dir,
        name=name,
        origin_hash=origin_hash,
        bundled_from=bundled_from,
        bundled_version=bundled_version,
    )


def _read_provenance(skill_dir: Path) -> dict[str, object]:
    try:
        data = json.loads((skill_dir / BUNDLED_SKILL_PROVENANCE_FILE_NAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _backup_existing_skill(target_dir: Path, *, skills_root: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = skills_root / RESTORE_BACKUPS_DIR_NAME / timestamp / target_dir.name
    while backup_dir.exists():
        backup_dir = skills_root / RESTORE_BACKUPS_DIR_NAME / f"{timestamp}-{uuid4().hex[:8]}" / target_dir.name
    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(target_dir, backup_dir, ignore=_copy_ignore)
    return backup_dir


def _is_opted_out(pulsara_home: Path) -> bool:
    return (pulsara_home / BUNDLED_OPT_OUT_MARKER_NAME).exists()


def _is_ignored_hash_path(relative_path: str) -> bool:
    parts = relative_path.split("/")
    return (
        BUNDLED_SKILL_PROVENANCE_FILE_NAME in parts
        or "__pycache__" in parts
        or relative_path.endswith(".pyc")
        or ".DS_Store" in parts
    )


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _compact_dict(data: dict[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in data.items() if value is not None}


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None
