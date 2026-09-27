#!/usr/bin/env python3
"""Read-only OPC/Word package inspection; not an OOXML schema validator.

Uses namespace-aware standard-library parsers for package facts and a limited
text projection. It never extracts ZIP entries to disk or follows links.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import posixpath
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
REV = {"ins", "del", "moveFrom", "moveTo", "pPrChange", "rPrChange",
       "tblPrChange", "trPrChange", "tcPrChange", "sectPrChange",
       "numberingChange", "cellIns", "cellDel", "cellMerge"}


def visible(element: ET.Element, view: str) -> bool:
    omitted = {"del", "moveFrom"} if view == "final" else {"ins", "moveTo"}
    return element.tag not in {W + name for name in omitted}


def text_of(element: ET.Element, view: str) -> str:
    if not visible(element, view):
        return ""
    if element.tag in {W + x for x in ("drawing", "pict", "object", "txbxContent")}:
        return ""
    if element.tag == W + "t" or (element.tag == W + "delText" and view == "original"):
        return element.text or ""
    if element.tag == W + "tab":
        return "\t"
    if element.tag in {W + "br", W + "cr"}:
        return "\n"
    if element.tag == W + "noBreakHyphen":
        return "\u2011"
    if element.tag == W + "softHyphen":
        return "\u00ad"
    for kind in ("footnote", "endnote", "comment"):
        if element.tag == W + kind + "Reference":
            return f"[{kind}:{element.get(W + 'id', '')}]"
    return "".join(text_of(child, view) for child in element)


def children(parent: ET.Element, wanted: str, view: str):
    """Walk transparent block wrappers, without flattening tables/paragraphs."""
    wrappers = {W + n for n in ("sdt", "sdtContent", "customXml", "ins", "del", "moveFrom", "moveTo")}
    for child in parent:
        if not visible(child, view):
            continue
        if child.tag == W + wanted:
            yield child
        elif child.tag in wrappers:
            yield from children(child, wanted, view)


def blocks(parent: ET.Element, view: str):
    for child in parent:
        if not visible(child, view):
            continue
        if child.tag == W + "p":
            style = child.find(W + "pPr/" + W + "pStyle")
            yield {"kind": "paragraph", "style": None if style is None else style.get(W + "val"),
                   "text": text_of(child, view)}
        elif child.tag == W + "tbl":
            rows = []
            for row in children(child, "tr", view):
                cells = []
                for cell in children(row, "tc", view):
                    span = cell.find(W + "tcPr/" + W + "gridSpan")
                    merge = cell.find(W + "tcPr/" + W + "vMerge")
                    cells.append({"grid_span": 1 if span is None else span.get(W + "val"),
                                  "vertical_merge": None if merge is None else merge.get(W + "val", "continue"),
                                  "blocks": list(blocks(cell, view))})
                rows.append(cells)
            yield {"kind": "table", "rows": rows}
        elif child.tag in {W + n for n in ("footnote", "endnote", "comment")}:
            if child.get(W + "type", "normal") != "normal":
                continue
            yield {"kind": child.tag[len(W):], "id": child.get(W + "id"),
                   "author": child.get(W + "author"), "blocks": list(blocks(child, view))}
        elif child.tag in {W + n for n in ("sdt", "sdtContent", "customXml", "ins", "del", "moveFrom", "moveTo")}:
            yield from blocks(child, view)


def relationship_source(name: str) -> str | None:
    if name == "_rels/.rels":
        return ""
    path = Path(name)
    if path.parent.name != "_rels" or not name.endswith(".rels"):
        return None
    return (path.parent.parent / path.name[:-5]).as_posix()


def internal_target(source: str, target: str) -> str:
    uri = urlsplit(target)
    if uri.scheme or uri.netloc:
        raise ValueError("Internal relationship has an absolute URI")
    raw = unquote(uri.path)
    if not raw:
        return source
    joined = raw.lstrip("/") if raw.startswith("/") else posixpath.join(posixpath.dirname(source), raw)
    result = posixpath.normpath(joined)
    if result == ".." or result.startswith("../") or "\\" in result:
        raise ValueError("Relationship target escapes the package")
    return result


def inspect(path: Path, *, include_text=False, part=None, view="final", start=0, end=None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    roots: dict[str, ET.Element] = {}
    with zipfile.ZipFile(path) as package:
        entries = [name for name in package.namelist() if not name.endswith("/")]
        names = set(entries)
        for name, count in Counter(entries).items():
            if count != 1:
                errors.append(f"Duplicate package entry: {name}")
        bad = package.testzip()
        if bad:
            errors.append(f"ZIP CRC failure: {bad}")
        for name in entries:
            if name.endswith((".xml", ".rels")):
                try:
                    roots[name] = ET.fromstring(package.read(name))
                except ET.ParseError as exc:
                    errors.append(f"Malformed XML in {name}: {exc}")
        types = roots.get("[Content_Types].xml")
        if types is None:
            errors.append("Missing or unreadable [Content_Types].xml")
        else:
            defaults = {e.get("Extension", "").lower() for e in types.findall(CT + "Default")}
            overrides = {unquote(e.get("PartName", "")).lstrip("/") for e in types.findall(CT + "Override")}
            for name in sorted(names - {"[Content_Types].xml"}):
                if name not in overrides and name.rsplit(".", 1)[-1].lower() not in defaults:
                    errors.append(f"No registered content type: {name}")
        main = None
        relation_ids: dict[str, set[str]] = {}
        external = []
        for name, root in roots.items():
            if not name.endswith(".rels"):
                continue
            source = relationship_source(name)
            if source is None:
                errors.append(f"Unrecognized relationships part location: {name}")
                continue
            if source and source not in names:
                errors.append(f"Relationships source missing: {source}")
            ids = relation_ids.setdefault(source, set())
            for rel in root.findall(PKG + "Relationship"):
                rid = rel.get("Id", "")
                if not rid or rid in ids:
                    errors.append(f"Missing/duplicate relationship ID in {name}: {rid}")
                ids.add(rid)
                target = rel.get("Target", "")
                if rel.get("TargetMode") == "External":
                    external.append({"source": source, "id": rid, "target": target})
                    continue
                try:
                    resolved = internal_target(source, target)
                except ValueError as exc:
                    errors.append(f"{name}/{rid}: {exc}")
                    continue
                if resolved not in names:
                    errors.append(f"Missing relationship target: {name}/{rid} -> {resolved}")
                if source == "" and rel.get("Type", "").endswith("/officeDocument"):
                    main = resolved
        if main is None:
            errors.append("No internal package officeDocument relationship")
        for name, root in roots.items():
            for node in root.iter():
                for attr in (R + "id", R + "embed", R + "link"):
                    if attr in node.attrib and node.attrib[attr] not in relation_ids.get(name, set()):
                        errors.append(f"Unresolved relationship ID: {name}: {node.attrib[attr]}")
        feature_tags = {"paragraphs": W + "p", "tables": W + "tbl", "text_boxes": W + "txbxContent",
                        "drawings": W + "drawing", "legacy_pictures": W + "pict", "objects": W + "object",
                        "content_controls": W + "sdt", "alt_chunks": W + "altChunk", "complex_field_markers": W + "fldChar",
                        "simple_fields": W + "fldSimple", "equations": M + "oMath", "hidden_runs": W + "vanish",
                        "symbols": W + "sym"}
        stories = []
        revisions = Counter()
        nodes = []
        for name, root in roots.items():
            if root.tag not in {W + n for n in ("document", "hdr", "ftr", "footnotes", "endnotes", "comments")}:
                continue
            story_nodes = list(root.iter())
            nodes.extend(story_nodes)
            counts = Counter(n.tag for n in story_nodes)
            features = {key: counts[tag] for key, tag in feature_tags.items() if counts[tag]}
            stories.append({"part": name, "features": features})
            revisions.update(n.tag[len(W):] for n in story_nodes if n.tag.startswith(W) and n.tag[len(W):] in REV)
        for kind in ("comment", "footnote", "endnote"):
            definitions = [n.get(W + "id") for n in nodes if n.tag == W + kind]
            for rid, count in Counter(definitions).items():
                if rid is None or count > 1:
                    errors.append(f"Missing/duplicate {kind} ID: {rid}")
            reference_tags = {W + kind + "Reference"}
            if kind == "comment":
                reference_tags |= {W + "commentRangeStart", W + "commentRangeEnd"}
            refs = {n.get(W + "id") for n in nodes if n.tag in reference_tags}
            for rid in sorted(refs - set(definitions), key=str):
                errors.append(f"Dangling {kind} reference: {rid}")
        starts = {n.get(W + "id") for n in nodes if n.tag == W + "commentRangeStart"}
        ends = {n.get(W + "id") for n in nodes if n.tag == W + "commentRangeEnd"}
        if starts != ends:
            warnings.append("Comment range starts/ends differ; inspect the anchor structure")
        if starts or ends:
            warnings.append("Comment contents and reference markers are available; anchored ranges are not resolved by this helper")
        main_root = roots.get(main or "")
        if main_root is not None and main_root.tag != W + "document":
            warnings.append("Main part is not transitional WordprocessingML; this helper cannot project its text")
        if revisions:
            warnings.append("Revisions present: text views handle supported inline wrappers only; paragraph/table/property semantics require separate verification")
        features_all = Counter(n.tag for n in nodes)
        for label in ("text_boxes", "drawings", "legacy_pictures", "objects", "alt_chunks", "symbols", "equations"):
            if features_all[feature_tags[label]]:
                warnings.append(f"{label}: inspect separately; ordinary text output does not reconstruct this content")
        if any("commentsExtended" in n or "commentsExtensible" in n for n in names):
            warnings.append("Modern comment extensions present; thread/resolved state is not interpreted")
        if any(n.endswith("vbaProject.bin") for n in names):
            warnings.append("Macro project present; this helper does not execute it")
        style_names = {}
        for root in roots.values():
            if root.tag == W + "styles":
                for style in root.findall(W + "style"):
                    name = style.find(W + "name")
                    style_names[style.get(W + "styleId")] = None if name is None else name.get(W + "val")
        used_styles = Counter(n.get(W + "val") for n in nodes if n.tag == W + "pStyle")
        result = {"input": str(path.resolve()), "package_checks_passed": not errors,
                  "validation_scope": "ZIP, XML, content-type coverage, relationships, basic note/comment references; not XSD or layout validation",
                  "main_part": main, "parts": sorted(names), "stories": stories,
                  "explicit_paragraph_styles": [{"id": sid, "name": style_names.get(sid), "uses": count}
                                                for sid, count in used_styles.items()],
                  "revision_types": dict(revisions), "external_relationships": external,
                  "errors": errors, "warnings": warnings}
        if include_text:
            selected = part or main
            root = roots.get(selected or "")
            if root is None:
                errors.append(f"Requested story part unavailable: {selected}")
            elif root.tag not in {W + n for n in ("document", "hdr", "ftr", "footnotes", "endnotes", "comments")}:
                errors.append(f"Unsupported story root: {selected}: {root.tag}")
            else:
                container = root.find(W + "body") if root.tag == W + "document" else root
                if container is None:
                    errors.append(f"Missing story body: {selected}")
                else:
                    extracted = list(blocks(container, view))
                    selected_blocks = extracted[start:end]
                    result["text"] = {"part": selected, "view": view, "total_blocks": len(extracted),
                                      "start": start, "end": start + len(selected_blocks),
                                      "blocks": [{"index": start + i, **block} for i, block in enumerate(selected_blocks)],
                                      "limitations": "Cached field values; hidden text retained; drawing/text-box/object/equation content excluded; complex revisions not projected"}
        result["package_checks_passed"] = not errors
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--part")
    parser.add_argument("--view", choices=("final", "original"), default="final")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int)
    args = parser.parse_args()
    if args.start < 0 or (args.end is not None and args.end < args.start):
        parser.error("Require 0 <= start <= end (end exclusive)")
    try:
        result = inspect(args.input, include_text=args.text, part=args.part,
                         view=args.view, start=args.start, end=args.end)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        print(json.dumps({"input": str(args.input), "error": str(exc),
                          "hint": "This inspector needs unencrypted DOCX; use a DOC-capable reader for binary DOC, or decrypt encrypted input when needed"}, ensure_ascii=False))
        return 2
    if args.text:
        result = {key: result[key] for key in
                  ("input", "package_checks_passed", "validation_scope", "errors", "warnings", "text")
                  if key in result}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["package_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
