# Pulsara Sheets — source notes

Attribution for maintainers; not required to use this Skill. Instructions and helpers are original; no upstream Skill scripts are vendored. Third-party references retain their respective licenses.

| Reference | Areas referenced |
| --- | --- |
| OpenAI Codex Spreadsheets, installed package 26.915.20218 | Workbook structure, editable formulas/charts, scoped structural and visual verification. |
| OpenAI Codex Excel Live Control, installed package 26.915.20218 | Native application capabilities and explicit connection requirements. |
| [Anthropic XLSX](https://github.com/anthropics/skills/tree/main/skills/xlsx) | Formula/cache separation, recalculation and error inspection. |
| [NousResearch Hermes XLSX](https://github.com/NousResearch/hermes-agent/tree/main/skills/productivity/xlsx) | Optional focused helpers, typed cells, structural-edit impact lists. |
| [Harvey Labs XLSX](https://github.com/harveyai/harvey-labs/tree/main/lab_core/harness/skills/xlsx) | Calculation and package verification as separate concerns. |
| [MiniMax XLSX](https://github.com/MiniMax-AI/skills/tree/main/skills/minimax-xlsx) | Task routing, targeted package edits, static-check boundaries. |
| [jwynia XLSX Generator](https://github.com/jwynia/agent-skills/tree/main/skills/general/document-processing/spreadsheet/xlsx-generator) | Separate template inspection, filling, and creation; typed replacements. |
| [borghei XLSX Toolkit](https://github.com/borghei/Claude-Skills/tree/main/documents/xlsx-toolkit) | Read-only feature inventory and external-reference visibility. |
| [Vasiliy Uvarov Document XLSX](https://github.com/vasilyu1983/AI-Agents-public/tree/main/frameworks/shared-skills/skills/document-xlsx) | Focused references for native charts, tables, validation, and pivots. |
| [PracticalSwan Excel Sheet](https://github.com/PracticalSwan/agent-skills/tree/main/excel-sheet) | CSV types, text interoperability, and usable sheet formatting. |
| [DSL Builders Spreadsheet](https://github.com/dsl-builders/spreadsheet-skill) | Inspectable selected-range results and explicit tool capability boundaries. |
| [Claude Office Skills XLSX](https://github.com/claude-office-skills/skills/tree/main/xlsx-manipulation) | Compact cell, formula, style, and chart examples. |

Primary library documentation:

- [openpyxl loading and saving](https://openpyxl.readthedocs.io/en/stable/tutorial.html), [structural edits](https://openpyxl.readthedocs.io/en/stable/editing_worksheets.html), [optimized modes](https://openpyxl.readthedocs.io/en/stable/optimized.html).
- [XlsxWriter formulas](https://xlsxwriter.readthedocs.io/working_with_formulas.html), [charts](https://xlsxwriter.readthedocs.io/working_with_charts.html), [memory modes](https://xlsxwriter.readthedocs.io/working_with_memory.html).
- [xlrd scope](https://xlrd.readthedocs.io/en/latest/), [python-calamine](https://github.com/dimastbk/python-calamine), [msoffcrypto-tool](https://msoffcrypto-tool.readthedocs.io/en/latest/).
- [LibreOffice conversion filters](https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html).
