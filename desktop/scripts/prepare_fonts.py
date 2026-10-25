# /// script
# requires-python = ">=3.12"
# dependencies = ["fonttools[woff]==4.60.1"]
# ///
"""Rebuild the redistributable fonts from one pinned Google Fonts revision.

FontTools owns OpenType/WOFF2 parsing and serialization. No glyph subsetting,
design changes, system-font lookup, runtime download or platform fonts enter
the application. The complete upstream OFL notices travel with the assets.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request

from fontTools.ttLib import TTFont

REVISION = "e7db6ca603fd4862010383ff17ffd6cc4ffac1c0"
UPSTREAM = f"https://raw.githubusercontent.com/google/fonts/{REVISION}/ofl"
ROOT = Path(__file__).resolve().parents[2]
FACES = (
    ("inter", "Inter[opsz,wght].ttf", "inter-roman"),
    ("inter", "Inter-Italic[opsz,wght].ttf", "inter-italic"),
    ("sourceserif4", "SourceSerif4[opsz,wght].ttf", "source-serif-roman"),
    ("sourceserif4", "SourceSerif4-Italic[opsz,wght].ttf", "source-serif-italic"),
    ("notosanssc", "NotoSansSC[wght].ttf", "noto-sans-sc"),
    ("notoserifsc", "NotoSerifSC[wght].ttf", "noto-serif-sc"),
    ("jetbrainsmono", "JetBrainsMono[wght].ttf", "jetbrains-mono-roman"),
    ("jetbrainsmono", "JetBrainsMono-Italic[wght].ttf", "jetbrains-mono-italic"),
    ("gelasio", "Gelasio[wght].ttf", "gelasio-roman"),
)


def fetch(family: str, name: str) -> bytes:
    url = f"{UPSTREAM}/{family}/{urllib.parse.quote(name)}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def prepare(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    notices = destination / "licenses"
    notices.mkdir(exist_ok=True)
    families = sorted({family for family, _, _ in FACES})
    for family in families:
        license_text = fetch(family, "OFL.txt")
        if b"SIL OPEN FONT LICENSE" not in license_text:
            raise ValueError(f"Missing upstream OFL notice: {family}")
        (notices / f"{family}-OFL.txt").write_bytes(license_text)
    records = []
    with tempfile.TemporaryDirectory(prefix="pulsara-open-fonts-") as temporary:
        for family, filename, stem in FACES:
            source = Path(temporary) / filename
            source.write_bytes(fetch(family, filename))
            with TTFont(source, recalcTimestamp=False) as font:
                cmap = font.getBestCmap()
                glyphs = font.getGlyphOrder()
                record = {
                    "file": f"{stem}.woff2",
                    "family": font["name"].getDebugName(1),
                    "postScriptName": font["name"].getDebugName(6),
                    "version": font["name"].getDebugName(5),
                    "unicodeCodepoints": len(cmap),
                    "glyphs": len(glyphs),
                    "axes": {axis.axisTag: [axis.minValue, axis.defaultValue, axis.maxValue]
                             for axis in font["fvar"].axes},
                    "upstream": f"{UPSTREAM}/{family}/{urllib.parse.quote(filename)}",
                }
                font.flavor = "woff2"
                output = destination / record["file"]
                font.save(output)
            with TTFont(output) as converted:
                if converted.getBestCmap() != cmap or converted.getGlyphOrder() != glyphs:
                    raise ValueError(f"WOFF2 conversion changed glyph coverage: {filename}")
            record["bytes"] = output.stat().st_size
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
    # Build provenance and licenses, not runtime font-discovery state.
    sources = (
        "# Bundled open fonts\n\n"
        "All fonts are from Google Fonts at this immutable revision:\n\n"
        f"https://github.com/google/fonts/tree/{REVISION}\n\n"
        "FontTools 4.60.1 converts the full upstream variable TTFs to WOFF2. "
        "No glyph subsetting or outline/design changes are made. Font family, "
        "copyright and license name tables remain in the font files. "
        "Conversion verifies the complete Unicode cmap and glyph order.\n\n"
        "Each *-OFL.txt is the complete corresponding upstream notice, including "
        "its copyright and any reserved font names. No Apple/macOS font file is bundled.\n\n"
        "To rebuild from the repository root:\n\n"
        "    uv run --no-project desktop/scripts/prepare_fonts.py\n\n"
        "## Faces\n\n"
        + "\n".join(f"- `{r['file']}` — {r['family']}, {r['version']}; [source]({r['upstream']})." for r in records)
        + "\n"
    )
    (notices / "SOURCES.md").write_text(sources, encoding="utf-8")
    print(json.dumps({"files": len(records), "bytes": sum(r["bytes"] for r in records)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "desktop/assets/fonts")
    prepare(parser.parse_args().output)
