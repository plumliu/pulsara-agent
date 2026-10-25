# Exact font notice and metadata review

2026-10-26. **No license/name mismatch found in the selected fixed-commit assets.** Source approval is limited to these actual six notices and nine converted files; artifact/load qualification awaits root's build.

I fetched each complete OFL notice from Google Fonts commit `e7db6ca603fd4862010383ff17ffd6cc4ffac1c0` into memory and compared complete bytes to the actual canonical `desktop/assets/fonts/licenses/*-OFL.txt`. All six match exactly and contain the complete license. `font-legal-metadata-review.json` records that comparison, copyright headers, actual WOFF2 name/version/copyright/license tables, axes and coverage. No per-file digest was added.

| Actual notice | Copyright holder/header | Declared RFN in that notice |
| --- | --- | --- |
| Inter | Inter Project Authors, 2020 | none |
| Source Serif 4 | Source Serif 4 Project Authors, 2014 | none |
| Noto Sans SC | Adobe, 2014–2021 | Source |
| Noto Serif SC | Google Inc., 2012 | none |
| JetBrains Mono | JetBrains Mono Project Authors, 2020 | none |
| Gelasio | Gelasio Project Authors, 2022 | none |

The selected Source Serif 4 **standalone OFL file** does not reserve Source. However, actual delivered Source Serif 4 nameID0 still contains Adobe's Source RFN notice; that is an observed binary fact, not an inference from another release. Noto Sans SC also retains its Source notice, while its delivered primary/typographic family is Noto Sans SC (legacy default-style family includes Thin). Record both actual notices accurately; do not claim that the entire Source Serif delivery has no RFN statement. CSS aliases such as Pulsara Body are application font-face selectors, not a replacement for embedded metadata or a legal rename.

I read `prepare_fonts.py`: it uses the maintained FontTools 4.60.1 parser/serializer, changes flavor to WOFF2, does no subset/design/name-table editing, and verifies full cmap and glyph order after saving. I independently opened all nine delivered WOFF2s using root's existing uv script environment (repository environment has no FontTools; no dependency install or refresh was performed). All decode as WOFF2, retain copyright and license name tables, and the total is exactly **20,545,940 bytes**. The independently read family/version/axes/cmap-count/glyph-count values agree with root's conversion JSONL. This is a readback review, not a second conversion or claim that metadata count alone proves outline equivalence.

The canonical source note identifies fixed upstream filenames/revision, FontTools version, conversion procedure and unmodified coverage. Full six-family notices remain adjacent. The original Vite plugin now emits those seven human-readable files under `assets/font-licenses/`; that is the existing build-owner seam, not a runtime registry. It must still be checked in emitted static assets, wheel and the guest Python payload. Electron's separate `assets/**` inclusion carries the canonical startup copy and notices into asar.

[Official OFL](https://openfontlicense.org/open-font-license-official-text/) permits redistribution bundled with software while carrying its notice/license, and limits reserved names for modified versions. Its [official FAQ §2.2/2.2.1](https://openfontlicense.org/ofl-faq/) specifically permits unchanged WOFF/WOFF2 compression without a name change when original data remains unchanged except compression and WOFF metadata is absent or preserves corresponding original metadata. The [official webfont guidance](https://openfontlicense.org/webfonts-and-reserved-font-names/) explicitly applies that principle to WOFF2. Therefore format conversion is not by itself a reason to block this compression-only candidate or demand all families be renamed; actual data/metadata preservation is the relevant check. No Apple font is copied.

No production/source assets were changed by this review. No build, settings, app, PG or Git operation was performed. Final browser/platform-font/HTTP/wheel/asar checks remain root's pending evidence gate, alongside the independently tested MIME change.

Additional independent source readback now passed for all nine fixed-upstream TTFs versus delivered WOFF2s: every complete name-record tuple and the raw name-table bytes are identical; WOFF-specific metadata is omitted. See `font-name-table-readback.json`. Source inputs were read into memory and no assets were regenerated. Together with root's full cmap/glyph-order readback and compression-only script, this closes the metadata/RFN preservation question without renaming or removing original notices.
