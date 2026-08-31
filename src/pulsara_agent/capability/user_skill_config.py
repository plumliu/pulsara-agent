"""Path-based user Skill enablement stored in the Pulsara home."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Mapping

import yaml

from pulsara_agent.capability.pulsara_home import require_pulsara_home


USER_SKILL_CONFIG_NAME = "skills.yaml"
MAXIMUM_USER_SKILL_CONFIG_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class UserSkillConfigEntry:
    path: Path
    enabled: bool

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("user Skill config path must be absolute")


@dataclass(frozen=True, slots=True)
class UserSkillConfigSnapshot:
    config_path: Path
    entries: tuple[UserSkillConfigEntry, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        paths = tuple(item.path for item in self.entries)
        if paths != tuple(sorted(paths, key=str)) or len(paths) != len(set(paths)):
            raise ValueError("user Skill config entries are not ordered and unique")
        if self.error is not None and self.entries:
            raise ValueError("unavailable user Skill config contains partial entries")

    @property
    def available(self) -> bool:
        return self.error is None

    def enabled_for(self, path: Path) -> bool:
        """Return the path rule, defaulting to enabled and failing closed."""

        if not self.available:
            return False
        normalized = _normalize_skill_path(path)
        for item in self.entries:
            if item.path == normalized:
                return item.enabled
        return True


def default_user_skill_config_path() -> Path:
    return require_pulsara_home() / USER_SKILL_CONFIG_NAME


def load_user_skill_config(
    *, config_path: Path | None = None
) -> UserSkillConfigSnapshot:
    path = (config_path or default_user_skill_config_path()).expanduser()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return UserSkillConfigSnapshot(config_path=path)
    except OSError:
        return _unavailable(path)
    if len(data) > MAXIMUM_USER_SKILL_CONFIG_BYTES:
        return _unavailable(path)
    try:
        decoded = data.decode("utf-8")
        loaded = yaml.safe_load(decoded)
        raw = {} if loaded is None else loaded
        if not isinstance(raw, Mapping) or set(raw) - {"skills"}:
            raise ValueError("user Skill config root is invalid")
        values = raw.get("skills", [])
        if not isinstance(values, list):
            raise ValueError("user Skill config entries are invalid")
        entries: dict[Path, UserSkillConfigEntry] = {}
        for value in values:
            if not isinstance(value, Mapping) or set(value) != {"path", "enabled"}:
                raise ValueError("user Skill config entry is invalid")
            raw_path = value.get("path")
            enabled = value.get("enabled")
            if not isinstance(raw_path, str) or not isinstance(enabled, bool):
                raise ValueError("user Skill config entry fields are invalid")
            skill_path = _normalize_skill_path(Path(raw_path))
            if skill_path in entries:
                raise ValueError("user Skill config path is duplicated")
            entries[skill_path] = UserSkillConfigEntry(skill_path, enabled)
    except (UnicodeError, ValueError, yaml.YAMLError):
        return _unavailable(path)
    return UserSkillConfigSnapshot(
        config_path=path,
        entries=tuple(sorted(entries.values(), key=lambda item: str(item.path))),
    )


def set_user_skill_enabled(
    *,
    skill_path: Path,
    enabled: bool,
    config_path: Path | None = None,
) -> Path:
    """Atomically upsert one exact path rule in the user configuration."""

    snapshot = load_user_skill_config(config_path=config_path)
    if not snapshot.available:
        raise ValueError(snapshot.error)
    normalized = _normalize_skill_path(skill_path)
    entries = {item.path: item for item in snapshot.entries}
    entries[normalized] = UserSkillConfigEntry(normalized, enabled)
    encoded = yaml.safe_dump(
        {
            "skills": [
                {"path": str(item.path), "enabled": item.enabled}
                for item in sorted(entries.values(), key=lambda item: str(item.path))
            ]
        },
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")
    if len(encoded) > MAXIMUM_USER_SKILL_CONFIG_BYTES:
        raise ValueError("技能配置内容过多，无法保存。")
    path = snapshot.config_path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


def _normalize_skill_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError("user Skill config path must be absolute")
    return expanded.resolve(strict=False)


def _unavailable(path: Path) -> UserSkillConfigSnapshot:
    return UserSkillConfigSnapshot(
        config_path=path,
        error="技能开关配置暂时无法读取，请检查 skills.yaml。",
    )


__all__ = [
    "MAXIMUM_USER_SKILL_CONFIG_BYTES",
    "USER_SKILL_CONFIG_NAME",
    "UserSkillConfigEntry",
    "UserSkillConfigSnapshot",
    "default_user_skill_config_path",
    "load_user_skill_config",
    "set_user_skill_enabled",
]
