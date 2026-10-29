from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pulsara_agent.desktop_app.session_controller import _create_quick_workspace_root


def test_quick_workspace_root_uses_readable_timestamp_and_short_random_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_root = tmp_path / "workspaces"
    managed_root.mkdir()
    now = datetime(2026, 8, 31, 13, 42, 7, tzinfo=timezone.utc)
    tokens = iter(
        (
            "a1b2c3d4" + "0" * 24,
            "a1b2c3d4" + "1" * 24,
            "deadbeef" + "2" * 24,
        )
    )
    monkeypatch.setattr(
        "pulsara_agent.desktop_app.session_controller.uuid4",
        lambda: SimpleNamespace(hex=next(tokens)),
    )

    first = _create_quick_workspace_root(managed_root, now)
    second = _create_quick_workspace_root(managed_root, now)

    assert first.name == "quick-20260831-134207-a1b2c3d4"
    assert second.name == "quick-20260831-134207-deadbeef"
    assert first.is_dir() and second.is_dir()
