"""Shared absolute-only resolution for the Pulsara user home."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path


PULSARA_HOME_ENV = "PULSARA_HOME"
_ENVIRONMENT_VALUE = object()


class PulsaraHomeDisposition(StrEnum):
    RESOLVED = "RESOLVED"
    INVALID = "INVALID"


class PulsaraHomeUnavailableReason(StrEnum):
    RELATIVE_PULSARA_HOME = "RELATIVE_PULSARA_HOME"
    USER_HOME_UNAVAILABLE = "USER_HOME_UNAVAILABLE"
    PATH_RESOLUTION_UNAVAILABLE = "PATH_RESOLUTION_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class UserHomeResolution:
    """One call-local observation of the current OS user's home directory."""

    disposition: PulsaraHomeDisposition
    path: Path | None = None
    unavailable_reason: PulsaraHomeUnavailableReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, PulsaraHomeDisposition):
            raise TypeError("user home disposition is not closed")
        resolved = self.disposition is PulsaraHomeDisposition.RESOLVED
        if resolved != (self.path is not None):
            raise ValueError("user home path conflicts with disposition")
        if resolved == (self.unavailable_reason is not None):
            raise ValueError("user home unavailable reason conflicts")
        if self.path is not None and not self.path.is_absolute():
            raise ValueError("user home resolution is not absolute")


@dataclass(frozen=True, slots=True)
class PulsaraHomeResolution:
    disposition: PulsaraHomeDisposition
    path: Path | None = None
    unavailable_reason: PulsaraHomeUnavailableReason | None = None
    used_default: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, PulsaraHomeDisposition):
            raise TypeError("Pulsara home disposition is not closed")
        resolved = self.disposition is PulsaraHomeDisposition.RESOLVED
        if resolved != (self.path is not None):
            raise ValueError("Pulsara home path conflicts with disposition")
        if resolved == (self.unavailable_reason is not None):
            raise ValueError("Pulsara home unavailable reason conflicts")
        if self.path is not None and not self.path.is_absolute():
            raise ValueError("Pulsara home resolution is not absolute")
        if not resolved and self.used_default:
            raise ValueError("invalid Pulsara home cannot be marked default")


class PulsaraHomeResolutionError(ValueError):
    """Typed failure for consumers that require a usable Pulsara home."""

    def __init__(self, resolution: PulsaraHomeResolution) -> None:
        if resolution.disposition is not PulsaraHomeDisposition.INVALID:
            raise ValueError("Pulsara home error requires an invalid resolution")
        self.resolution = resolution
        reason = resolution.unavailable_reason
        super().__init__(reason.value if reason is not None else "PULSARA_HOME_INVALID")


class UserHomeResolutionError(ValueError):
    """Typed failure for consumers that require the frozen OS user home."""

    def __init__(self, resolution: UserHomeResolution) -> None:
        if resolution.disposition is not PulsaraHomeDisposition.INVALID:
            raise ValueError("user home error requires an invalid resolution")
        self.resolution = resolution
        reason = resolution.unavailable_reason
        super().__init__(reason.value if reason is not None else "USER_HOME_INVALID")


def resolve_user_home() -> UserHomeResolution:
    """Observe and lexically normalize ``Path.home()`` exactly once."""

    try:
        user_home = Path.home()
        if not user_home.is_absolute():
            return _invalid_user_home()
        return UserHomeResolution(
            PulsaraHomeDisposition.RESOLVED,
            path=_normalize_absolute(user_home),
        )
    except (MemoryError, OSError, RuntimeError, ValueError):
        return _invalid_user_home()


def resolve_pulsara_home(
    raw_value: str | None | object = _ENVIRONMENT_VALUE,
    *,
    user_home_resolution: UserHomeResolution | None = None,
) -> PulsaraHomeResolution:
    """Resolve one absolute Pulsara home without interpreting relative cwd.

    ``raw_value`` is injectable for application-service and test callers.  When
    omitted, the current process environment is observed exactly once.
    """

    try:
        raw = (
            os.getenv(PULSARA_HOME_ENV)
            if raw_value is _ENVIRONMENT_VALUE
            else raw_value
        )
    except (MemoryError, OSError, RuntimeError):
        return _invalid(PulsaraHomeUnavailableReason.PATH_RESOLUTION_UNAVAILABLE)
    if raw is not None and not isinstance(raw, str):
        raise TypeError("PULSARA_HOME input must be text or None")
    configured = (raw or "").strip()
    if not configured:
        user_home = user_home_resolution or resolve_user_home()
        if user_home.disposition is PulsaraHomeDisposition.INVALID:
            return _invalid(PulsaraHomeUnavailableReason.USER_HOME_UNAVAILABLE)
        if user_home.path is None:  # pragma: no cover - closed resolution invariant
            return _invalid(PulsaraHomeUnavailableReason.USER_HOME_UNAVAILABLE)
        try:
            return PulsaraHomeResolution(
                PulsaraHomeDisposition.RESOLVED,
                path=_normalize_absolute(user_home.path / ".pulsara"),
                used_default=True,
            )
        except (MemoryError, OSError, RuntimeError, ValueError):
            return _invalid(PulsaraHomeUnavailableReason.PATH_RESOLUTION_UNAVAILABLE)

    try:
        if configured == "~" or configured.startswith("~/"):
            user_home = user_home_resolution or resolve_user_home()
            if user_home.disposition is PulsaraHomeDisposition.INVALID:
                return _invalid(PulsaraHomeUnavailableReason.USER_HOME_UNAVAILABLE)
            if user_home.path is None:  # pragma: no cover - closed invariant
                return _invalid(PulsaraHomeUnavailableReason.USER_HOME_UNAVAILABLE)
            suffix = configured.removeprefix("~").lstrip("/")
            expanded = user_home.path / suffix
        else:
            expanded = Path(configured).expanduser()
    except (MemoryError, OSError, RuntimeError):
        return _invalid(PulsaraHomeUnavailableReason.PATH_RESOLUTION_UNAVAILABLE)
    if not expanded.is_absolute():
        return _invalid(PulsaraHomeUnavailableReason.RELATIVE_PULSARA_HOME)
    try:
        normalized = _normalize_absolute(expanded)
    except (MemoryError, OSError, RuntimeError, ValueError):
        return _invalid(PulsaraHomeUnavailableReason.PATH_RESOLUTION_UNAVAILABLE)
    return PulsaraHomeResolution(
        PulsaraHomeDisposition.RESOLVED,
        path=normalized,
        used_default=False,
    )


def require_pulsara_home(
    raw_value: str | None | object = _ENVIRONMENT_VALUE,
) -> Path:
    resolution = resolve_pulsara_home(raw_value)
    if resolution.path is None:
        raise PulsaraHomeResolutionError(resolution)
    return resolution.path


def _normalize_absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("absolute path required")
    # ``normpath`` is lexical.  Unlike ``Path.resolve()``, it never interprets
    # a configured relative value against the invocation cwd and does not make
    # a missing Pulsara home an error.
    return Path(os.path.normpath(os.fspath(path)))


def _invalid(reason: PulsaraHomeUnavailableReason) -> PulsaraHomeResolution:
    return PulsaraHomeResolution(
        PulsaraHomeDisposition.INVALID,
        unavailable_reason=reason,
    )


def _invalid_user_home() -> UserHomeResolution:
    return UserHomeResolution(
        PulsaraHomeDisposition.INVALID,
        unavailable_reason=PulsaraHomeUnavailableReason.USER_HOME_UNAVAILABLE,
    )


__all__ = [
    "PULSARA_HOME_ENV",
    "PulsaraHomeDisposition",
    "PulsaraHomeResolution",
    "PulsaraHomeResolutionError",
    "PulsaraHomeUnavailableReason",
    "UserHomeResolution",
    "UserHomeResolutionError",
    "require_pulsara_home",
    "resolve_pulsara_home",
    "resolve_user_home",
]
