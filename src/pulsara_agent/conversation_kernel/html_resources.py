"""Freeze the declared local resources of one HTML into its existing blob.

html5lib/tinycss2 own syntax. This adapter owns only the supported resource
slots, file-relative addressing and Pulsara's existing resource boundaries.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
from time import monotonic
from urllib.parse import unquote

import html5lib
import tinycss2
from tinycss2.ast import StringToken
from tinycss2.serializer import _serialize_to

from pulsara_agent.conversation_kernel.blob import MAXIMUM_BLOB_BYTES
from pulsara_agent.conversation_kernel.prompt_content import (
    MAXIMUM_PROMPT_MULTIPART_BYTES,
)
from pulsara_agent.conversation_kernel.visualization import read_visualization_text
from pulsara_agent.tools.builtins.filesystem import read_image_file_bounded

_HTML_NS = "http://www.w3.org/1999/xhtml"
_HTML_SPACE = " \t\n\f\r"
_CLASSIC_TYPES = frozenset(
    {
        "",
        "text/javascript",
        "application/javascript",
        "application/ecmascript",
        "text/ecmascript",
        "application/x-javascript",
        "application/x-ecmascript",
        "text/javascript1.0",
        "text/javascript1.1",
        "text/javascript1.2",
        "text/javascript1.3",
        "text/javascript1.4",
        "text/javascript1.5",
        "text/jscript",
        "text/livescript",
        "text/x-javascript",
        "text/x-ecmascript",
    }
)


@dataclass(frozen=True, slots=True)
class LocalHtmlReadCandidate:
    payload: bytes
    access_path: Path
    requested_path: str


class HtmlResourceError(ValueError):
    """Expected per-item failure, with a path but never resource source code."""


def _check_deadline(deadline_monotonic):
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("HTML resource preparation deadline expired")


class _BoundedText:
    def __init__(self, deadline):
        self.output = BytesIO()
        self.deadline = deadline

    def write(self, text):
        _check_deadline(self.deadline)
        encoded = text.encode("utf-8")
        if self.output.tell() + len(encoded) > MAXIMUM_BLOB_BYTES:
            raise HtmlResourceError("HTML_RESOURCES_TOO_LARGE")
        self.output.write(encoded)

    def bytes(self):
        return self.output.getvalue()


def _path(value, base):
    # Check URL syntax before decoding. Decoded names are filesystem names,
    # never reinterpreted as schemes or ~ / ${PULSARA_HOME} expansions.
    if value.lower().startswith("data:"):
        return None
    if (
        not value
        or value.startswith("//")
        or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value)
        or "?" in value
        or "#" in value
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
    ):
        raise HtmlResourceError("HTML_RESOURCE_ADDRESS_UNSUPPORTED")
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeError as exc:
        raise HtmlResourceError("HTML_RESOURCE_ADDRESS_INVALID") from exc
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        raise HtmlResourceError("HTML_RESOURCE_ADDRESS_INVALID")
    path = Path(decoded)
    return path if path.is_absolute() else base / path


def _data_url(body, media_type, *, deadline_monotonic):
    _check_deadline(deadline_monotonic)
    prefix = f"data:{media_type};base64,"
    if len(prefix) + 4 * ((len(body) + 2) // 3) > MAXIMUM_BLOB_BYTES:
        raise HtmlResourceError("HTML_RESOURCES_TOO_LARGE")
    return prefix + base64.b64encode(body).decode("ascii")


def _read(path, kind, *, deadline_monotonic):
    _check_deadline(deadline_monotonic)
    path = path.resolve()
    if kind == "image":
        return read_image_file_bounded(
            path, maximum_bytes=MAXIMUM_PROMPT_MULTIPART_BYTES
        )
    result = read_visualization_text(path, kind=kind.upper(), allow_empty=True)
    if result is None:
        raise HtmlResourceError(f"{kind.upper()}_NOT_FOUND")
    return result


def _parse_html(body, *, deadline_monotonic):
    _check_deadline(deadline_monotonic)
    return html5lib.parse(body.decode("utf-8"), treebuilder="dom", scripting=True)


def _serialize_html(document, *, deadline_monotonic):
    writer = _BoundedText(deadline_monotonic)
    serializer = html5lib.serializer.HTMLSerializer(
        quote_attr_values="always",
        omit_optional_tags=False,
        escape_lt_in_attrs=True,
    )
    for chunk in serializer.serialize(html5lib.getTreeWalker("dom")(document)):
        writer.write(chunk)
    return writer.bytes()


def _url_value(token):
    if token.type == "url":
        return token.value
    if token.type == "function" and token.lower_name == "url":
        args = [x for x in token.arguments if x.type not in {"whitespace", "comment"}]
        if len(args) != 1 or args[0].type != "string":
            raise HtmlResourceError("CSS_URL_INVALID")
        return args[0].value
    return None


def _css_slots(text, inline, *, deadline_monotonic):
    _check_deadline(deadline_monotonic)
    nodes = (
        tinycss2.parse_blocks_contents(text)
        if inline
        else tinycss2.parse_stylesheet(text)
    )
    slots = []
    # Rule parsing identifies import/font contexts; token traversal does not
    # guess resource addresses from strings or implement a CSS grammar.
    pending = [("rules", nodes, "image")]
    while pending:
        _check_deadline(deadline_monotonic)
        mode, items, kind = pending.pop()
        for node in items:
            if mode == "tokens":
                value = _url_value(node)
                if value is not None:
                    slots.append((node, value, kind))
                elif node.type == "function":
                    pending.append(("tokens", node.arguments, kind))
                elif hasattr(node, "content"):
                    pending.append(("tokens", node.content, kind))
            elif node.type == "at-rule":
                if node.lower_at_keyword == "import":
                    prelude = [
                        x
                        for x in node.prelude
                        if x.type not in {"whitespace", "comment"}
                    ]
                    value = (
                        prelude[0].value
                        if prelude and prelude[0].type == "string"
                        else _url_value(prelude[0])
                        if prelude
                        else None
                    )
                    if value is None or not value.lower().startswith("data:"):
                        raise HtmlResourceError("CSS_IMPORT_UNSUPPORTED")
                else:
                    pending.append(("tokens", node.prelude, "image"))
                    if node.content is not None:
                        pending.append(
                            (
                                "rules",
                                tinycss2.parse_blocks_contents(node.content),
                                "font"
                                if node.lower_at_keyword == "font-face"
                                else kind,
                            )
                        )
            elif node.type == "qualified-rule":
                pending.append(("tokens", node.prelude, "image"))
                pending.append(
                    ("rules", tinycss2.parse_blocks_contents(node.content), kind)
                )
            elif node.type == "declaration":
                pending.append(
                    (
                        "tokens",
                        node.value,
                        "font"
                        if kind == "font" and node.lower_name == "src"
                        else "image",
                    )
                )
    return nodes, slots


def _replace_css_url(token, value):
    if token.type == "url":
        token.value = value
        token.representation = f'url("{value}")'
    else:
        token.arguments = [
            StringToken(token.source_line, token.source_column, value, f'"{value}"')
        ]


def _serialize_css(nodes, *, deadline_monotonic):
    writer = _BoundedText(deadline_monotonic)
    # tinycss2's public serialize() collects an unbounded list before joining.
    # Its writer primitive supplies the same grammar/escaping with our existing
    # blob boundary enforced during construction; no serializer is copied.
    _serialize_to(nodes, writer.write)
    return writer.bytes().decode("utf-8")


async def freeze_html_resources(
    body: bytes, access_path: Path, *, io, validator, deadline_monotonic: float
) -> bytes:
    """Use the caller's IO, image validator and deadline; retain no durable state."""

    async def run(operation, *args):
        return await io.run(operation, *args, deadline_monotonic=deadline_monotonic)

    document = await run(_parse_html, body)
    cache = {}
    html_size = len(body)
    changed = False

    def replace_size(old, new):
        nonlocal html_size, changed
        html_size += len(new.encode("utf-8")) - len(old.encode("utf-8"))
        if html_size > MAXIMUM_BLOB_BYTES:
            raise HtmlResourceError("HTML_RESOURCES_TOO_LARGE")
        changed |= old != new

    async def resource(value, base, kind):
        path = _path(value, base)
        if path is None:
            return value
        # Access path is part of the CSS cache key: aliases in different
        # directories may share bytes yet refer to different relative images.
        key = (path, kind)
        if key not in cache:
            try:
                payload = await run(_read, path, kind)
                media_type = {
                    "css": "text/css;charset=utf-8",
                    "js": "text/javascript;charset=utf-8",
                }.get(kind)
                if kind == "image":
                    if validator is None:
                        raise RuntimeError("HTML image validation owner is unavailable")
                    image = await validator.freeze_local_image(
                        payload, deadline_monotonic=deadline_monotonic
                    )
                    payload, media_type = image.immutable_bytes, image.media_type
                elif kind == "css":
                    payload = (
                        await css(payload.decode("utf-8-sig"), path.parent, False)
                    ).encode("utf-8")
                cache[key] = await run(_data_url, payload, media_type)
            except TimeoutError:
                raise
            except (OSError, ValueError) as exc:
                # Do not include body text or raw OS errors in the UI error.
                code = (
                    str(exc) if isinstance(exc, ValueError) else "RESOURCE_READ_FAILED"
                )
                raise HtmlResourceError(f"{code} ({path.name})") from exc
        return cache[key]

    async def css(text, base, inline):
        nodes, slots = await run(_css_slots, text, inline)
        size = len(text.encode("utf-8"))
        edited = False
        for token, value, kind in slots:
            if value.lower().startswith("data:"):
                continue
            if kind == "font":
                raise HtmlResourceError("CSS_FONT_UNSUPPORTED")
            if value.startswith("#"):
                continue
            new = await resource(value, base, "image")
            size += len(new.encode("utf-8")) - len(value.encode("utf-8"))
            if size > MAXIMUM_BLOB_BYTES:
                raise HtmlResourceError("HTML_RESOURCES_TOO_LARGE")
            _replace_css_url(token, new)
            edited = True
        return await run(_serialize_css, nodes) if edited else text

    pending = [document]
    while pending:
        _check_deadline(deadline_monotonic)
        element = pending.pop()
        pending.extend(reversed(element.childNodes))
        if element.nodeType != element.ELEMENT_NODE or element.namespaceURI != _HTML_NS:
            continue
        tag = element.localName
        attribute, kind = None, None
        if tag == "img" and element.getAttribute("src").strip(_HTML_SPACE):
            attribute, kind = "src", "image"
        elif tag == "link" and "stylesheet" in re.split(
            r"[ \t\n\f\r]+", element.getAttribute("rel").lower()
        ):
            attribute, kind = "href", "css"
        elif tag == "script" and element.hasAttribute("src"):
            # An explicit type (including empty) overrides the historical
            # language attribute. Unknown languages are inert data blocks.
            script_type = element.getAttribute("type")
            if not element.hasAttribute("type") and element.getAttribute("language"):
                script_type = "text/" + element.getAttribute("language")
            script_type = script_type.strip(_HTML_SPACE).lower()
            if script_type == "module":
                if not element.getAttribute("src").lower().startswith("data:"):
                    raise HtmlResourceError("HTML_MODULE_UNSUPPORTED")
            elif script_type in _CLASSIC_TYPES:
                attribute, kind = "src", "js"
        if attribute:
            value = element.getAttribute(attribute)
            if element.hasAttribute("integrity") and not value.lower().startswith(
                "data:"
            ):
                raise HtmlResourceError("HTML_RESOURCE_INTEGRITY_UNSUPPORTED")
            new = await resource(value, access_path.parent, kind)
            replace_size(value, new)
            element.setAttribute(attribute, new)
        if element.hasAttribute("style"):
            value = element.getAttribute("style")
            new = await css(value, access_path.parent, True)
            replace_size(value, new)
            element.setAttribute("style", new)
        if tag == "style" and element.getAttribute("type").lower() in {"", "text/css"}:
            value = "".join(
                child.data
                for child in element.childNodes
                if child.nodeType == child.TEXT_NODE
            )
            new = await css(value, access_path.parent, False)
            if new != value:
                # CSS serialization may decode \3c into '<'. Raw-text HTML
                # does not recognize entities, so use a CSS-equivalent escape.
                new = re.sub(r"<(?=/style)", r"\\3c ", new, flags=re.IGNORECASE)
                replace_size(value, new)
                for child in list(element.childNodes):
                    element.removeChild(child)
                element.appendChild(document.createTextNode(new))
    return await run(_serialize_html, document) if changed else body
