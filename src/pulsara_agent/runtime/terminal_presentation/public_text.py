"""The sole transform from untrusted text to terminal-safe public text."""

from __future__ import annotations


def terminal_safe_public_text(value: str) -> str:
    """Render terminal control code points as inert, visible ASCII escapes."""

    if not isinstance(value, str):
        raise TypeError("terminal public text must be a string")
    parts: list[str] = []
    for character in value:
        code_point = ord(character)
        if character in {"\t", "\n"}:
            parts.append(character)
        elif code_point < 0x20 or 0x7F <= code_point <= 0x9F:
            parts.append(f"\\x{code_point:02X}")
        else:
            parts.append(character)
    return "".join(parts)


def bounded_terminal_safe_public_text(
    value: str,
    *,
    maximum_code_points: int,
    maximum_utf8_bytes: int,
) -> str:
    """Apply the transform, then truncate without splitting a code point."""

    if maximum_code_points < 1 or maximum_utf8_bytes < 1:
        raise ValueError("terminal public text bounds must be positive")
    safe = terminal_safe_public_text(value)
    result: list[str] = []
    encoded_bytes = 0
    for character in safe:
        if len(result) >= maximum_code_points:
            break
        size = len(character.encode("utf-8"))
        if encoded_bytes + size > maximum_utf8_bytes:
            break
        result.append(character)
        encoded_bytes += size
    return "".join(result)


__all__ = ["bounded_terminal_safe_public_text", "terminal_safe_public_text"]
