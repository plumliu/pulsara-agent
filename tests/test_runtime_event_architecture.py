from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "src" / "pulsara_agent" / "runtime"
TOOLS_DIR = REPO_ROOT / "src" / "pulsara_agent" / "tools"


def test_runtime_business_code_does_not_directly_append_to_event_log() -> None:
    append_violations: list[str] = []
    extend_violations: list[str] = []

    for path in sorted(RUNTIME_DIR.rglob("*.py")) + sorted(TOOLS_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        normalized = path.as_posix()
        if "event_log.append(" in text and not normalized.endswith("runtime/session.py"):
            append_violations.append(normalized)
        if "event_log.extend(" in text:
            extend_violations.append(normalized)

    assert append_violations == []
    assert extend_violations == []


def test_runtime_business_code_does_not_use_hook_manager_dispatch_as_main_path() -> None:
    violations: list[str] = []

    for path in sorted(RUNTIME_DIR.rglob("*.py")):
        normalized = path.as_posix()
        if normalized.endswith("runtime/hooks.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if "dispatch_observer_event(" in text:
            violations.append(normalized)

    assert violations == []
