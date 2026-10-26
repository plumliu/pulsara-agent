# /// script
# requires-python = ">=3.12"
# dependencies = ["fonttools[woff]==4.60.1"]
# ///
"""Rebuild Google Fonts faces and copy the official unmodified MiSans WOFF2s.

FontTools owns OpenType/WOFF2 parsing and serialization. No glyph subsetting,
design changes, system-font lookup, runtime download or platform fonts enter
the application. Complete upstream licenses travel with the assets.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request
from zipfile import ZipFile

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

MISANS_URL = "https://hyperos.mi.com/font-download/MiSans.zip"
# CSS semantic weights match the approved Regular=400 gallery. MiSans's
# internal OS/2 weight numbers differ; leave the official binaries untouched.
MISANS_FACES = (
    ("Thin", 100), ("ExtraLight", 200), ("Light", 300), ("Normal", 350),
    ("Regular", 400), ("Medium", 500), ("Demibold", 550),
    ("Semibold", 600), ("Bold", 700), ("Heavy", 900),
)


def copy_misans(destination: Path, archive: Path) -> None:
    with ZipFile(archive) as package:
        for name, _weight in MISANS_FACES:
            data = package.read(f"MiSans/woff2/MiSans-{name}.woff2")
            if not data.startswith(b"wOF2"):
                raise ValueError(f"Invalid official MiSans WOFF2: {name}")
            (destination / f"misans-{name.lower()}.woff2").write_bytes(data)
    license_source = ROOT / "desktop/assets/fonts/licenses/MiSans-license.pdf"
    license_target = destination / "licenses/MiSans-license.pdf"
    if license_source.resolve() != license_target.resolve():
        shutil.copyfile(license_source, license_target)


def fetch(family: str, name: str) -> bytes:
    url = f"{UPSTREAM}/{family}/{urllib.parse.quote(name)}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def prepare(destination: Path, misans_archive: Path | None = None) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    notices = destination / "licenses"
    notices.mkdir(exist_ok=True)
    families = sorted({family for family, _, _ in FACES})
    for family in families:
        license_text = fetch(family, "OFL.txt")
        if b"SIL OPEN FONT LICENSE" not in license_text:
            raise ValueError(f"Missing upstream OFL notice: {family}")
        (notices / f"{family}-OFL.txt").write_bytes(license_text)
    with tempfile.TemporaryDirectory(prefix="pulsara-misans-") as temporary:
        archive = misans_archive
        if archive is None:
            archive = Path(temporary) / "MiSans.zip"
            with urllib.request.urlopen(MISANS_URL, timeout=60) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
        copy_misans(destination, archive)
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
        "# Bundled fonts\n\n"
        "Inter, Noto Sans SC, Noto Serif SC, Source Serif 4, JetBrains Mono and Gelasio are from Google Fonts at this immutable revision:\n\n"
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
    sources += (
        "\n## MiSans\n\nPulsara uses MiSans fonts. Copyright belongs to Xiaomi Inc. "
        "The complete MiSans Font Intellectual Property License Agreement is in "
        "`MiSans-license.pdf`. It is not an OFL license.\n\n"
        f"Original WOFF2 files copied unchanged from {MISANS_URL} . "
        "No conversion, subsetting or outline changes. Distributed only as part of Pulsara.\n"
        "Official license: https://hyperos.mi.com/font/en/download/ .\n\n"
        "CSS weights match the approved gallery (Regular=400); original binary weight metadata is preserved.\n"
        + "\n".join(f"- `misans-{name.lower()}.woff2` — {name}, CSS weight {weight}." for name, weight in MISANS_FACES)
        + "\n"
    )
    (notices / "SOURCES.md").write_text(sources, encoding="utf-8")
    files = [destination / r["file"] for r in records] + [destination / f"misans-{name.lower()}.woff2" for name, _ in MISANS_FACES]
    print(json.dumps({"files": len(files), "bytes": sum(path.stat().st_size for path in files)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "desktop/assets/fonts")
    parser.add_argument("--misans-archive", type=Path, help="Local official MiSans.zip; otherwise download from Xiaomi")
    options = parser.parse_args()
    prepare(options.output, options.misans_archive)
