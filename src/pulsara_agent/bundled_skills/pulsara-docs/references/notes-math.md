# Notes and equations

## Native footnotes and endnotes

Use native notes for editable, linked references. Pandoc can generate them from prose markup; existing documents need an editor/library or a local OOXML edit that preserves their note structure.

For OOXML work, keep story references linked to the correct note part; allocate non-colliding IDs and preserve separator/continuation-separator entries. Register new parts and relationships, retain styles and section numbering/restart settings, and verify actual rendered numbering and placement. An internal note ID is not its displayed number. The inspector checks dangling references, not all note semantics.

## Word equations

Prefer editable OMML unless another representation was requested. Use a maintained converter or native editor supporting the source notation. Preserve existing equation representations during unrelated edits. Check mathematical meaning, punctuation, units, and inline/display layout: an equation image may look correct while failing an editability requirement.

The bundled text inspector does not extract OMML equations. Inspect those separately or render them when the task depends on their content.

## MathType only when requested

MathType OLE work needs a compatible integration, commonly Windows desktop Word, installed MathType, and COM/OLE support. Python packages or LibreOffice alone do not provide this capability.

Inventory actual equations/OLE objects and identify intended text conversions in context. Avoid broad identifier matching or notation rewrites without a mathematical reason. Preserve operators, indices, and meaning.

Verify object type, embedding relationships, preview appearance, full-page layout, and editability in the target application when promised. If the platform is unavailable, preserve existing equations and report the unperformed conversion.
