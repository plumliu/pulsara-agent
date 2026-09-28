# Pulsara PDF — source notes

Reference notes for maintainers, not required reading to use the Skill. Instructions and the small example are original; no upstream Skill scripts are vendored. Third-party projects retain their own licenses.

| Reference | Areas referenced |
| --- | --- |
| [OpenAI PDF skill](https://github.com/openai/skills/blob/main/skills/.curated/pdf/SKILL.md) | Combine content checks with rendered-page inspection; choose creation tools for the output. |
| [Community pdf-extraction](https://github.com/claude-office-skills/skills/blob/main/pdf-extraction/SKILL.md) | Positioned text, regional extraction, table debugging, and source-page provenance. |
| [Community pdf-processing-pro](https://github.com/henkisdabro/wookstar-claude-plugins/blob/main/plugins/documents/skills/pdf-processing-pro/SKILL.md) | Keep extraction and verification guidance available by task. |

Primary technical references:

- [pdfplumber](https://github.com/jsvine/pdfplumber): coordinates, cropped pages, table objects, and visual debugging.
- [pypdf text extraction](https://pypdf.readthedocs.io/en/stable/user/extract-text.html): text order, scanned documents, and OCR limitations.
- [PyMuPDF image recipes](https://pymupdf.readthedocs.io/en/latest/recipes-images.html): page rendering, embedded images, and masks.
- [ReportLab user guide](https://www.reportlab.com/docs/reportlab-userguide.pdf): flowables, fonts, paragraph markup, and canvas layout.
- [OCRmyPDF introduction](https://ocrmypdf.readthedocs.io/en/latest/introduction.html): searchable PDF text layers; an optional route, not a required dependency.
