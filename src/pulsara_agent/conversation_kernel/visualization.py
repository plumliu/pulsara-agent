"""Inline visualization marker parsing and immutable publication values."""

from __future__ import annotations
from dataclasses import dataclass, replace
from enum import StrEnum
import os
from pathlib import Path
import re
import stat
from time import monotonic
from typing import Callable, Iterable, Mapping

from pulsara_agent.llm.input import LLMImagePart
from pulsara_agent.identity import is_valid_id
from pulsara_agent.conversation_kernel.repository_errors import (
    ConversationKernelConflict,
)

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
    image: LLMImagePart | None = None
    failure_code: str | None = None
    failure_detail: str | None = None

    def __post_init__(self) -> None:
        if self.state is VisualizationOccurrenceState.READY:
            if (
                (bool(self.html) == (self.image is not None))
                or self.failure_code
                or self.failure_detail
            ):
                raise ValueError("READY visualization has invalid content")
        elif (
            self.html is not None
            or self.image is not None
            or not self.failure_code
            or not self.failure_detail
        ):
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
    return read_visualization_text(path, kind="HTML", allow_empty=False)


def read_visualization_text(path: Path, *, kind: str, allow_empty: bool) -> bytes | None:
    """Bounded UTF-8 sources; None means only a reliable not-found."""
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        facts = os.fstat(descriptor)
        if not stat.S_ISREG(facts.st_mode):
            raise ValueError(f"{kind}_NOT_REGULAR")
        if facts.st_size > MAXIMUM_BLOB_BYTES:
            raise ValueError(f"{kind}_TOO_LARGE")
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
            raise ValueError(f"{kind}_TOO_LARGE")
        if not body and not allow_empty:
            raise ValueError(f"{kind}_EMPTY")
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{kind}_NOT_UTF8") from exc
        return body
    except FileNotFoundError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


async def materialize_visualizations(
    body: str,
    resolve: Callable[[str], Path],
    *,
    io,
    validator,
    image_reader,
    session_id: str,
    workspace_id: str,
    deadline_monotonic: float,
) -> tuple[FrozenVisualizationOccurrence, ...]:
    from pulsara_agent.conversation_kernel.prompt_content import (
        bind_prompt_images,
        MAXIMUM_PROMPT_MULTIPART_BYTES,
    )
    from pulsara_agent.conversation_kernel.prompt_storage import (
        CanonicalImageReferenceUnavailable,
        CanonicalImageReferenceResourceExceeded,
    )
    from pulsara_agent.conversation_kernel.image_validation import (
        PromptImageValidationError,
    )
    from pulsara_agent.llm.input import FrozenPromptContent
    from pulsara_agent.conversation_kernel.html_resources import freeze_html_resources

    results = []
    cache = {}
    for value in visualization_paths(body):
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("visualization publication deadline expired")
        name = "图片" if value.startswith("image:") else Path(value).name
        key = None
        try:
            if value.startswith("image:"):
                if not is_valid_id(value, "image"):
                    raise ValueError("IMAGE_REFERENCE_INVALID")
                key = value
                if key in cache:
                    results.append(replace(cache[key], source_filename=name))
                    continue
                if image_reader is None:
                    raise RuntimeError("visualization image reader is unavailable")
                image = await io.run(
                    image_reader.read_image,
                    session_id=session_id,
                    workspace_id=workspace_id,
                    image_ref=value,
                    maximum_encoded_bytes=MAXIMUM_PROMPT_MULTIPART_BYTES,
                    deadline_monotonic=deadline_monotonic,
                )
                item = FrozenVisualizationOccurrence(
                    name, VisualizationOccurrenceState.READY, image=image
                )
            else:
                # Reserved schemes never fall back to paths. A literal colon in
                # a filename can be expressed with an explicit ./ path.
                if not value or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
                    raise ValueError("VISUALIZATION_SOURCE_INVALID")
                html_source = Path(value).suffix.lower() in {".html", ".htm"}
                if html_source:
                    validate_html_path(value)
                path = await io.run(
                    _resolve_source,
                    resolve,
                    value,
                    deadline_monotonic=deadline_monotonic,
                )
                key = (path, html_source)
                if key in cache:
                    results.append(replace(cache[key], source_filename=name))
                    continue
                payload = await io.run(
                    _read_source,
                    path,
                    html_source,
                    deadline_monotonic=deadline_monotonic,
                )
                if html_source:
                    payload = await freeze_html_resources(
                        payload, path, io=io, validator=validator,
                        deadline_monotonic=deadline_monotonic,
                    )
                    item = FrozenVisualizationOccurrence(
                        name, VisualizationOccurrenceState.READY, html=payload
                    )
                else:
                    if validator is None:
                        raise RuntimeError(
                            "visualization image validator is unavailable"
                        )
                    image = await validator.freeze_local_image(
                        payload, deadline_monotonic=deadline_monotonic
                    )
                    image = bind_prompt_images(
                        FrozenPromptContent((image,)), workspace_id=workspace_id
                    ).parts[0]
                    item = FrozenVisualizationOccurrence(
                        name, VisualizationOccurrenceState.READY, image=image
                    )
        except (TimeoutError, ConversationKernelConflict):
            raise
        except (
            ValueError,
            OSError,
            CanonicalImageReferenceUnavailable,
            CanonicalImageReferenceResourceExceeded,
            PromptImageValidationError,
        ) as exc:
            code = (
                "IMAGE_REFERENCE_UNAVAILABLE"
                if isinstance(exc, CanonicalImageReferenceUnavailable)
                else "IMAGE_RESOURCE_EXCEEDED"
                if isinstance(exc, CanonicalImageReferenceResourceExceeded)
                else "IMAGE_DECODE_FAILED"
                if isinstance(exc, PromptImageValidationError)
                else "VISUALIZATION_READ_FAILED"
                if isinstance(exc, OSError)
                else str(exc)
            )
            item = FrozenVisualizationOccurrence(
                name,
                VisualizationOccurrenceState.FAILED,
                failure_code=code,
                failure_detail=f"Visualization could not be displayed ({code}).",
            )
        if key is not None:
            cache[key] = item
        results.append(item)
    return tuple(results)


def _resolve_source(resolve, value, *, deadline_monotonic):
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("visualization publication deadline expired")
    return resolve(value)


def _read_source(path, html_source, *, deadline_monotonic):
    from pulsara_agent.tools.builtins.filesystem import read_image_file_bounded
    from pulsara_agent.conversation_kernel.prompt_content import (
        MAXIMUM_PROMPT_MULTIPART_BYTES,
    )

    if monotonic() >= deadline_monotonic:
        raise TimeoutError("visualization publication deadline expired")
    if html_source:
        body = read_visualization_file(path)
        if body is None:
            raise ValueError("HTML_NOT_FOUND")
        return body
    return read_image_file_bounded(path, maximum_bytes=MAXIMUM_PROMPT_MULTIPART_BYTES)


def visualization_metadata(row: Mapping[str, object]) -> dict:
    """Validate a saved occurrence using only canonical metadata, never payload."""
    result = {key: row[key] for key in ("ordinal", "state", "source_filename")}
    if row["state"] == "FAILED":
        if (
            row["blob_id"] is not None
            or row["width"] is not None
            or row["height"] is not None
            or not row["failure_code"]
            or not row["failure_detail"]
        ):
            raise ConversationKernelConflict("failed visualization facts drifted")
        return {
            **result,
            "failure_code": row["failure_code"],
            "failure_detail": row["failure_detail"],
        }
    if (
        row["state"] != "READY"
        or row["failure_code"] is not None
        or row["failure_detail"] is not None
    ):
        raise ConversationKernelConflict("visualization state drifted")
    image = row["media_type"] in {"image/png", "image/jpeg", "image/webp"}
    if (
        not is_valid_id(row["blob_id"], "image" if image else "blob")
        or (
            row["codec"] != "binary"
            if image
            else row["codec"] != VISUALIZATION_CODEC
            or row["media_type"] != VISUALIZATION_MEDIA_TYPE
        )
        or (
            not isinstance(row["width"], int)
            or row["width"] <= 0
            or not isinstance(row["height"], int)
            or row["height"] <= 0
            if image
            else row["width"] is not None or row["height"] is not None
        )
        or not isinstance(row["logical_size"], int)
        or row["logical_size"] <= 0
        or not isinstance(row["logical_digest"], str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", row["logical_digest"]) is None
    ):
        raise ConversationKernelConflict("visualization content facts drifted")
    from pulsara_agent.conversation_kernel.blob import _blob_id

    if row["blob_id"] != _blob_id(
        str(row["workspace_id"]),
        str(row["logical_digest"]),
        str(row["media_type"]),
        str(row["codec"]),
        image=image,
    ):
        raise ConversationKernelConflict("visualization identity drifted")
    result.update(
        content_digest=row["logical_digest"],
        content_size=row["logical_size"],
        media_type=row["media_type"],
        codec=row["codec"],
    )
    if image:
        from pulsara_agent.conversation_kernel.prompt_content import (
            CanonicalPromptImageDescriptor,
        )

        try:
            CanonicalPromptImageDescriptor(
                str(row["blob_id"]),
                str(row["logical_digest"]),
                row["logical_size"],
                str(row["media_type"]),
                row["width"],
                row["height"],
            )
        except ValueError as exc:
            raise ConversationKernelConflict(
                "visualization image descriptor drifted"
            ) from exc
        result.update(
            image_ref=row["blob_id"], width=row["width"], height=row["height"]
        )
    return result


def saved_visualizations(
    connection, session_id: str, entry_id: str
) -> tuple[dict, ...]:
    rows = connection.execute(
        """SELECT v.*, b.logical_digest, b.logical_size, b.media_type, b.codec
           FROM pulsara_v3.assistant_visualizations v
           JOIN pulsara_v3.transcript_entries e ON e.session_id=v.session_id
             AND e.workspace_id=v.workspace_id AND e.id=v.assistant_entry_id AND e.entry_kind='ASSISTANT_MESSAGE'
           LEFT JOIN pulsara_v3.blobs b ON b.id=v.blob_id AND b.workspace_id=v.workspace_id
           WHERE v.session_id=%s AND v.assistant_entry_id=%s ORDER BY v.ordinal""",
        (session_id, entry_id),
    ).fetchall()
    result = []
    for ordinal, row in enumerate(rows):
        if row["ordinal"] != ordinal:
            raise ConversationKernelConflict("visualization ordinals drifted")
        result.append(visualization_metadata(row))
    return tuple(result)
