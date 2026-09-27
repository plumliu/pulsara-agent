# /// script
# requires-python = ">=3.10"
# dependencies = ["python-docx>=1.2,<2"]
# ///
"""One guarded plain-body-paragraph replacement; optional native comment.

--expected-text locates one body paragraph by its complete original text.
Table-cell paragraphs are outside this example's scope.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.text.run import Run


def replace(doc, expected: str, old: str, new: str, *, comment=None, author="Automation"):
    matches = [paragraph for paragraph in doc.paragraphs if paragraph.text == expected]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one body paragraph matching --expected-text; found {len(matches)}. Inspect the target context.")
    paragraph = matches[0]
    if not old or sum(expected.startswith(old, i) for i in range(len(expected))) != 1:
        raise ValueError("Expected exactly one literal occurrence, including overlapping matches")
    if comment is not None and not new:
        raise ValueError("A deletion-only replacement has no new text to anchor the comment")
    allowed_p = {qn("w:pPr"), qn("w:r")}
    allowed_r = {qn("w:rPr"), qn("w:t")}
    if any(child.tag not in allowed_p for child in paragraph._p):
        raise ValueError("Complex paragraph: use a method that preserves its inline structures")
    if any(child.tag not in allowed_r for run in paragraph.runs for child in run._r):
        raise ValueError("Non-text run content: fields/objects/notes/breaks require a different method")
    if any(node.tag.rsplit("}", 1)[-1].endswith("Change") for node in paragraph._p.iter()):
        raise ValueError("Existing property revisions require a review-aware method")
    start, end = expected.index(old), expected.index(old) + len(old)
    spans, offset = [], 0
    for run in paragraph.runs:
        next_offset = offset + len(run.text)
        if offset < end and next_offset > start:
            spans.append((run, max(0, start - offset), min(len(run.text), end - offset)))
        offset = next_offset
    if not spans:
        raise ValueError("Cannot map target text to ordinary runs")
    first, first_start, _ = spans[0]
    last, _, last_end = spans[-1]
    fragments = []
    inserted = None
    for template, value, is_new in [(first, first.text[:first_start], False),
                                    (first, new, True), (last, last.text[last_end:], False)]:
        if not value:
            continue
        clone = deepcopy(template._r)
        fragment = Run(clone, paragraph)
        fragment.text = value
        fragments.append(clone)
        if is_new:
            inserted = fragment
    insertion_point = paragraph._p.index(first._r)
    for run, _, _ in spans:
        paragraph._p.remove(run._r)
    for i, node in enumerate(fragments):
        paragraph._p.insert(insertion_point + i, node)
    if comment is not None:
        doc.add_comment(inserted, text=comment, author=author)
    return paragraph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-text", required=True)
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--comment")
    parser.add_argument("--author", default="Automation")
    args = parser.parse_args()
    if args.output.exists() or args.input.resolve() == args.output.resolve():
        parser.error("This example writes a new file; select a separate, unused output")
    doc = Document(args.input)
    try:
        replace(doc, args.expected_text, args.old, args.new,
                comment=args.comment, author=args.author)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(args.output)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
