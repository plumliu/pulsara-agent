# Bundled fonts

The OFL fonts are from Google Fonts at this immutable revision:

https://github.com/google/fonts/tree/e7db6ca603fd4862010383ff17ffd6cc4ffac1c0

FontTools 4.60.1 converts the full upstream variable TTFs to WOFF2. No glyph subsetting or outline/design changes are made. Font family, copyright and license name tables remain in the font files. Conversion verifies the complete Unicode cmap and glyph order.

Each *-OFL.txt is the complete corresponding upstream notice, including its copyright and any reserved font names. No Apple/macOS font file is bundled.

To rebuild from the repository root:

    uv run --no-project desktop/scripts/prepare_fonts.py

## Faces

- `inter-roman.woff2` — Inter, Version 4.001;git-66647c0bb; [source](https://raw.githubusercontent.com/google/fonts/e7db6ca603fd4862010383ff17ffd6cc4ffac1c0/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf).
- `inter-italic.woff2` — Inter, Version 4.001;git-66647c0bb; [source](https://raw.githubusercontent.com/google/fonts/e7db6ca603fd4862010383ff17ffd6cc4ffac1c0/ofl/inter/Inter-Italic%5Bopsz%2Cwght%5D.ttf).
- `source-serif-roman.woff2` — Source Serif 4, Version 4.004;hotconv 1.0.116;makeotfexe 2.5.65601; [source](https://raw.githubusercontent.com/google/fonts/e7db6ca603fd4862010383ff17ffd6cc4ffac1c0/ofl/sourceserif4/SourceSerif4%5Bopsz%2Cwght%5D.ttf).
- `source-serif-italic.woff2` — Source Serif 4, Version 4.004;hotconv 1.0.116;makeotfexe 2.5.65601; [source](https://raw.githubusercontent.com/google/fonts/e7db6ca603fd4862010383ff17ffd6cc4ffac1c0/ofl/sourceserif4/SourceSerif4-Italic%5Bopsz%2Cwght%5D.ttf).
- `noto-sans-sc.woff2` — Noto Sans SC Thin, Version 2.004-H2;hotconv 1.0.118;makeotfexe 2.5.65603; [source](https://raw.githubusercontent.com/google/fonts/e7db6ca603fd4862010383ff17ffd6cc4ffac1c0/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf).
- `noto-serif-sc.woff2` — Noto Serif SC ExtraLight, Version 2.003-H1;hotconv 1.1.1;makeotfexe 2.6.0; [source](https://raw.githubusercontent.com/google/fonts/e7db6ca603fd4862010383ff17ffd6cc4ffac1c0/ofl/notoserifsc/NotoSerifSC%5Bwght%5D.ttf).
- `jetbrains-mono-roman.woff2` — JetBrains Mono, Version 2.211; [source](https://raw.githubusercontent.com/google/fonts/e7db6ca603fd4862010383ff17ffd6cc4ffac1c0/ofl/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf).
- `jetbrains-mono-italic.woff2` — JetBrains Mono, Version 2.211; [source](https://raw.githubusercontent.com/google/fonts/e7db6ca603fd4862010383ff17ffd6cc4ffac1c0/ofl/jetbrainsmono/JetBrainsMono-Italic%5Bwght%5D.ttf).
- `gelasio-roman.woff2` — Gelasio, Version 1.008; [source](https://raw.githubusercontent.com/google/fonts/e7db6ca603fd4862010383ff17ffd6cc4ffac1c0/ofl/gelasio/Gelasio%5Bwght%5D.ttf).

## MiSans

Pulsara uses MiSans fonts. Copyright belongs to Xiaomi Inc.; the complete
MiSans Font Intellectual Property License Agreement is in `MiSans-license.pdf`.
The official WOFF2 files are copied unchanged, without conversion or subsetting,
from https://hyperos.mi.com/font-download/MiSans.zip .
Official license and download page: https://hyperos.mi.com/font/en/download/ .
MiSans is distributed only as part of Pulsara, under its own license, not OFL.

CSS weights match the approved gallery (Regular=400); original binary weight metadata is preserved.
- `misans-thin.woff2` — CSS weight 100.
- `misans-extralight.woff2` — CSS weight 200.
- `misans-light.woff2` — CSS weight 300.
- `misans-normal.woff2` — CSS weight 350.
- `misans-regular.woff2` — CSS weight 400.
- `misans-medium.woff2` — CSS weight 500.
- `misans-demibold.woff2` — CSS weight 550.
- `misans-semibold.woff2` — CSS weight 600.
- `misans-bold.woff2` — CSS weight 700.
- `misans-heavy.woff2` — CSS weight 900.
