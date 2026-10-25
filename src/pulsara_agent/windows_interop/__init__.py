"""Trusted desktop configuration for task-local Windows interop ownership."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from threading import Lock


@dataclass(frozen=True, slots=True)
class InteropConfiguration:
    helper_path: Path
    distribution: str
    user: str
    python_executable: Path


_configuration: InteropConfiguration | None = None
_configuration_lock = Lock()


def configure_interop(*, helper_path: str, distribution: str, user: str) -> None:
    """Install the authenticated start-frame target once, never from tool input."""

    global _configuration
    if sys.platform != "linux" or os.getuid() == 0:
        raise RuntimeError("Windows interop requires the selected non-root Linux user")
    import pwd

    if pwd.getpwuid(os.getuid()).pw_name != user:
        raise ValueError("Windows interop user differs from the executing Linux user")
    if any(not value or "\0" in value for value in (helper_path, distribution, user)):
        raise ValueError("Windows interop target fields must be nonempty")
    helper = Path(helper_path)
    if (
        not helper.is_absolute()
        or not helper.is_file()
        or not os.access(helper, os.X_OK)
    ):
        raise ValueError("Windows interop helper must be an installed executable")
    value = InteropConfiguration(helper, distribution, user, Path(sys.executable))
    with _configuration_lock:
        if _configuration is not None and _configuration != value:
            raise RuntimeError("Windows interop target is already installed")
        _configuration = value


def create_scope():
    """Create a real Windows scope only for a configured Windows desktop kernel."""

    with _configuration_lock:
        configuration = _configuration
    if configuration is None:
        return None
    from pulsara_agent.windows_interop.scope import WindowsInteropScope

    return WindowsInteropScope(configuration)


__all__ = ["configure_interop", "create_scope"]
