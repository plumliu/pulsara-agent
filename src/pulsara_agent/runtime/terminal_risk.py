"""Terminal command risk classification for host-side guardrails."""

from __future__ import annotations

import re


_HARDLINE_RM_TARGET = (
    r"(?:/|/\*|~|\$HOME|"
    r"(?:/home|/etc|/usr|/var|/bin|/sbin|/lib|/lib64)(?:/\*?|\*)?)"
    r"(?=\s|$|[;&|])"
)

_HARDLINE_COMMAND_PATTERNS = [
    re.compile(
        r"(^|[;&|]\s*)rm\s+-[^\s]*[rR][^\s]*[fF][^\s]*\s+(?:--\s+)?"
        + _HARDLINE_RM_TARGET
    ),
    re.compile(
        r"(^|[;&|]\s*)rm\s+-[^\s]*[fF][^\s]*[rR][^\s]*\s+(?:--\s+)?"
        + _HARDLINE_RM_TARGET
    ),
    re.compile(r"(^|[;&|]\s*)dd\s+.*\bof=(/dev/(disk|sd|nvme|vd|mmcblk|hd)[^\s;&|]*)"),
    re.compile(r"(^|[;&|]\s*)mkfs(?:\.|\s|$)"),
    re.compile(r"(^|[;&|]\s*)(shutdown|reboot)(\s|$)"),
]

_RISKY_COMMAND_PATTERNS = [
    re.compile(r"(^|[;&|]\s*)rm\s+-[^\s]*[rR][^\s]*[fF][^\s]*(\s|$)"),
    re.compile(r"(^|[;&|]\s*)rm\s+-[^\s]*[fF][^\s]*[rR][^\s]*(\s|$)"),
    re.compile(r"(^|[;&|]\s*)sudo(\s|$)"),
    re.compile(r"(^|[;&|]\s*)chmod\s+-R(\s|$)"),
    re.compile(r"(^|[;&|]\s*)chown\s+-R(\s|$)"),
    re.compile(r"(^|[;&|]\s*)dd\s+.*\bof="),
    re.compile(r"(^|[;&|]\s*)ssh-keygen(\s|$)"),
]

_SENSITIVE_PATH_PATTERNS = [
    re.compile(r"(^|[\s'\"=:/])\.env(?:[\s'\";&|]|$)"),
    re.compile(r"(^|[\s'\"=])~?/\.ssh(?:/|[\s'\";&|]|$)"),
    re.compile(r"(^|[\s'\"=])~?/\.pulsara/config(?:\.|[\s'\";&|]|$)"),
    re.compile(r"(^|[\s'\"=])~?/\.(zshrc|bashrc|bash_profile|profile)(?:[\s'\";&|]|$)"),
    re.compile(r"(^|[\s'\"=])~?/\.(netrc|npmrc|pypirc)(?:[\s'\";&|]|$)"),
]


def is_hardline_terminal_command(command: str) -> bool:
    return _matches_any(_HARDLINE_COMMAND_PATTERNS, command.strip())


def is_risky_terminal_command(command: str) -> bool:
    stripped = command.strip()
    return _matches_any(_RISKY_COMMAND_PATTERNS, stripped) or is_sensitive_terminal_command(stripped)


def is_sensitive_terminal_command(command: str) -> bool:
    return _matches_any(_SENSITIVE_PATH_PATTERNS, command.strip())


def _matches_any(patterns: list[re.Pattern[str]], command: str) -> bool:
    return any(pattern.search(command) for pattern in patterns)
