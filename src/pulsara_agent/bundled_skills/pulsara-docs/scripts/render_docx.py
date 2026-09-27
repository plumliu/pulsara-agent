#!/usr/bin/env python3
"""Render a document via installed LibreOffice and Poppler; never installs tools."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def executable(explicit: str | None, name: str) -> str:
    if explicit:
        found = shutil.which(explicit)
        if found:
            return found
        raise FileNotFoundError(f"Executable unavailable: {explicit}")
    candidates = [name]
    if name == "soffice":
        candidates += ["libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"]
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            if os.environ.get(env):
                candidates.append(str(Path(os.environ[env]) / "LibreOffice/program/soffice.com"))
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    raise FileNotFoundError(f"Install/provide {name}; it is unavailable to this command")


def run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {command[0]}\n{result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


def render(source: Path, output: Path, *, soffice=None, pdftoppm=None, pages=None, dpi=110):
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("Input must be a file")
    output = output.expanduser().absolute()
    if output.exists():
        raise FileExistsError("Choose a new output directory; existing output is not overwritten")
    office = executable(soffice, "soffice")
    raster = executable(pdftoppm, "pdftoppm")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="word-preview-", dir=output.parent) as temp_name:
        temp = Path(temp_name)
        profile = temp / "profile"
        pdf_dir = temp / "pdf"
        pdf_dir.mkdir()
        generated = temp / "generated"
        generated.mkdir()
        office_log = run([office, f"-env:UserInstallation={profile.as_uri()}", "--headless",
                          "--convert-to", "pdf", "--outdir", str(pdf_dir), str(source)])
        pdf = pdf_dir / (source.stem + ".pdf")
        if not pdf.is_file() or pdf.stat().st_size == 0:
            raise RuntimeError(f"LibreOffice produced no non-empty PDF.\n{office_log}")
        command = [raster, "-png", "-r", str(dpi)]
        if pages:
            command += ["-f", str(pages[0]), "-l", str(pages[1])]
        command += [str(pdf), str(generated / "page")]
        raster_log = run(command)
        images = sorted(generated.glob("page-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
        if not images or any(p.stat().st_size == 0 for p in images):
            raise RuntimeError(f"Rasterizer produced no usable pages.\n{raster_log}")
        if pages and len(images) != pages[1] - pages[0] + 1:
            raise RuntimeError("Requested page range exceeds rendered PDF pages")
        shutil.copy2(pdf, generated / "preview.pdf")
        # Publish ordinary files into an exclusively-created directory.
        output.mkdir()
        for p in generated.iterdir():
            shutil.copy2(p, output / p.name)
    return {"source": str(source), "renderer": office, "pdf": str(output / "preview.pdf"),
            "images": [str(output / p.name) for p in images], "dpi": dpi,
            "page_range": pages, "visual_inspection_performed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--soffice")
    parser.add_argument("--pdftoppm")
    parser.add_argument("--pages", help="Inclusive one-based range, e.g. 3:5")
    parser.add_argument("--dpi", type=int, default=110)
    args = parser.parse_args()
    pages = None
    if args.pages:
        try:
            first, last = map(int, args.pages.split(":"))
            if not 1 <= first <= last:
                raise ValueError
            pages = (first, last)
        except ValueError:
            parser.error("--pages requires FIRST:LAST with 1 <= FIRST <= LAST")
    if args.dpi <= 0:
        parser.error("--dpi must be positive")
    try:
        result = render(args.input, args.out_dir, soffice=args.soffice,
                        pdftoppm=args.pdftoppm, pages=pages, dpi=args.dpi)
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
