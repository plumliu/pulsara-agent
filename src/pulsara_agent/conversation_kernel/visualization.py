"""Inline HTML marker parsing and immutable publication values."""

from __future__ import annotations
from dataclasses import dataclass, replace
from enum import StrEnum
import os
from pathlib import Path
import re
import stat
from time import monotonic
from typing import Callable, Iterable

from markdown_it import MarkdownIt
from markdown_it.rules_block.fence import make_fence_rule
from mdit_py_plugins.texmath.index import make_block_func
from pulsara_agent.conversation_kernel.blob import MAXIMUM_BLOB_BYTES

VISUALIZATION_MEDIA_TYPE = "text/html"
VISUALIZATION_CODEC = "utf-8"


def assistant_display_body(texts: Iterable[str]) -> str:
    """The same public TEXT projection used by annotations and the frontend."""
    return "\n\n".join(text for text in texts if text)


# The texmath extension point supplies block ownership/token emission; its
# default bracket regex accepts trailing prose and nested openers, unlike the
# existing remark-math-extended renderer. Configure just that lexical gap.
_bracket_rule = make_block_func(
    {
        "name": "math_bracket",
        "tag": r"\[",
        "tmpl": "{0}",
        "rex": re.compile(r"\\\[((?:[^\\]|\\\\|\\(?!\\|\[))*?)\\\][ \t]*(?=\n|$)"),
    }
)


def _math_bracket(state, start, end, silent):
    if state.is_code_block(start):
        return False
    source = state.src
    try:
        # Do not let the plugin match outside the containing Markdown block.
        state.src = source[: state.bMarks[end]]
        return _bracket_rule(state, start, end, silent)
    finally:
        state.src = source


def visualization_paths(body: str) -> tuple[str, ...]:
    # The library owns nesting, HTML/code blocks and math. Only the explicit
    # closing-fence check is ours: CommonMark also emits unclosed EOF fences.
    parser = MarkdownIt("commonmark")
    parser.block.ruler.before(
        "fence",
        "math_bracket",
        _math_bracket,
        {"alt": ["paragraph", "reference", "blockquote", "list"]},
    )
    # remark-math-extended treats dollar flow math like a fence, including
    # EOF closure. Configure the dependency's fence rule instead of cloning it.
    parser.block.ruler.before(
        "fence",
        "math_flow",
        make_fence_rule(
            markers=("$",),
            min_markers=2,
            token_type="math_flow",
            disallow_marker_in_info=("$",),
        ),
        {"alt": ["paragraph", "reference", "blockquote", "list"]},
    )
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    paths = []
    for token in parser.parse(body):
        if (
            token.type != "fence"
            or token.level != 0
            or token.info.strip(" \t") != "pulsara-visualization"
            or not token.map
        ):
            continue
        first, after = token.map
        closing = lines[after - 1] if after > first + 1 else ""
        if not re.fullmatch(
            r" {0,3}"
            + re.escape(token.markup[0])
            + "{"
            + str(len(token.markup))
            + r",}[ \t]*",
            closing,
        ):
            continue
        # One fence is one gallery; ordinal remains a flat per-message index.
        values = [line.strip(" \t") for line in token.content.split("\n")]
        paths.extend([value for value in values if value] or [""])
    return tuple(paths)


class VisualizationOccurrenceState(StrEnum):
    READY = "READY"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class FrozenVisualizationOccurrence:
    source_filename: str
    state: VisualizationOccurrenceState
    html: bytes | None = None
    failure_code: str | None = None
    failure_detail: str | None = None

    def __post_init__(self) -> None:
        if self.state is VisualizationOccurrenceState.READY:
            if not self.html or self.failure_code or self.failure_detail:
                raise ValueError("READY visualization has invalid content")
        elif self.html is not None or not self.failure_code or not self.failure_detail:
            raise ValueError("FAILED visualization has invalid content")


def validate_html_path(value: str) -> None:
    if (
        not value
        or "\n" in value
        or "\r" in value
        or "://" in value
        or "\0" in value
        or Path(value).suffix.lower() not in {".html", ".htm"}
    ):
        raise ValueError("HTML_PATH_INVALID")


def read_visualization_file(path: Path) -> bytes | None:
    """Return None only for a reliable not-found; every other failure is typed."""
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        facts = os.fstat(descriptor)
        if not stat.S_ISREG(facts.st_mode):
            raise ValueError("HTML_NOT_REGULAR")
        if facts.st_size > MAXIMUM_BLOB_BYTES:
            raise ValueError("HTML_TOO_LARGE")
        remaining = MAXIMUM_BLOB_BYTES + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) > MAXIMUM_BLOB_BYTES:
            raise ValueError("HTML_TOO_LARGE")
        if not body:
            raise ValueError("HTML_EMPTY")
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("HTML_NOT_UTF8") from exc
        return body
    except FileNotFoundError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def materialize_visualizations(
    body: str,
    resolve: Callable[[str], Path],
    *,
    deadline_monotonic: float = float("inf"),
) -> tuple[FrozenVisualizationOccurrence, ...]:
    results = []
    cache: dict[Path, FrozenVisualizationOccurrence] = {}
    for value in visualization_paths(body):
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("visualization publication deadline expired")
        name = Path(value).name if value and "\n" not in value else ""
        path = None
        try:
            validate_html_path(value)
            path = resolve(value)
            if path in cache:
                results.append(replace(cache[path], source_filename=name))
                continue
            html = read_visualization_file(path)
            if html is None:
                raise ValueError("HTML_NOT_FOUND")
            item = FrozenVisualizationOccurrence(
                name, VisualizationOccurrenceState.READY, html=html
            )
        except (ValueError, OSError) as exc:
            code = str(exc) if isinstance(exc, ValueError) else "HTML_READ_FAILED"
            item = FrozenVisualizationOccurrence(
                name,
                VisualizationOccurrenceState.FAILED,
                failure_code=code,
                failure_detail=f"Visualization could not be displayed ({code}).",
            )
        if path is not None:
            cache[path] = item
        results.append(item)
    return tuple(results)
