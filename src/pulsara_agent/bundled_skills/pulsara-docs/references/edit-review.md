# Edit and review

## Local edits

Locate the target by its story, surrounding context, and exact original text. Verify the intended occurrence before writing; re-resolve locations after structural changes. Word can split a phrase across differently formatted runs and inline objects.

For a small replacement, preserve unaffected runs and their formatting. Assigning `paragraph.text` or `cell.text` rebuilds their inline content; reserve it for intentional replacement of the whole structure. Treat hyperlinks, fields, bookmarks, note references, comments, and revisions as boundaries. Use a supported library API, or a local namespace-aware OOXML edit that preserves unrelated parts and relationships.

### Optional plain-paragraph example

[replace_text.py](../examples/replace_text.py) locates one ordinary **body paragraph by its complete original text**, then replaces one unique literal span. It accepts cross-run text; replacement text inherits the first affected run's formatting. Keep the matched span as small as the requested change permits.

```sh
uv run --script /path/to/pulsara-docs/examples/replace_text.py input.docx output.docx \
  --expected-text '付款期限为30天。' --old '30' --new '60' \
  --comment '已按双方协商调整' --author '项目组'
```

The comment is optional. The example refuses missing/duplicate paragraph matches, ambiguous spans (including overlaps), existing output files, and paragraphs containing complex inline structures. Tables are outside its scope. For those cases, use contextual task code or an appropriate editor rather than stripping structures to make the example work.

## Comments

Use `python-docx` 1.2+ for supported ordinary comments. Anchor the intended text at run boundaries; split plain runs when needed. Preserve existing IDs, authors, dates, anchors, and threads during unrelated edits. Replies and resolved states may need extension-part support.

For ordinary comment metadata, locate the intended comment in `doc.comments` and update properties such as `comment.author` through python-docx.

After editing comments, verify their contents and actual anchors while checking that body text stayed intact. For manual OOXML edits, also verify marker/reference IDs, relationships, and content types. A PDF without comment balloons cannot validate comments.

## Tracked changes

Apply the review mode requested by the user; existing revisions alone do not authorize accepting or rejecting them. For complex redlines or accept/reject operations, prefer native Word review/compare or a library supporting those revision types. `python-docx` is not a complete tracked-change engine.

For a constrained OOXML redline:

- Use separate insertion/deletion elements, non-colliding IDs, author/date metadata, deleted-text nodes, and significant-whitespace preservation.
- Mark only the changed span; preserve unaffected formatting, anchors, and prior revisions.
- Paragraph marks, row/cell operations, moves, and formatting changes have distinct semantics. Unwrapping every insertion/deletion is not a complete accept/reject implementation.
- Turning on track-revisions settings does not mark edits already made through a library.

Check that accepting the new revisions yields the requested result and rejecting them restores the original affected content, with prior revisions unchanged. Use an appropriate review view for semantics beyond ordinary text projection.

In reviewer summaries, tie each comment/revision to its passage and distinguish requested changes from completed decisions. For repair, preserve the source and report concrete losses; a repair dialog is not proof that content survived.
