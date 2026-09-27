# /// script
# requires-python = ">=3.10"
# dependencies = ["docxtpl>=0.20,<1", "python-docx>=1.2,<2"]
# ///
"""Fill a prepared docxtpl template from a JSON object; adapt to the task."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from docxtpl import DocxTemplate
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment


def fill(template: Path, values: dict, output: Path):
    if not isinstance(values, dict):
        raise ValueError("Template data must be a JSON object")
    if output.exists() or template.resolve() == output.resolve():
        raise FileExistsError("Choose a separate, unused output file")
    environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=True)
    document = DocxTemplate(template)
    document.render(values, jinja_env=environment, autoescape=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    document.save(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("values", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = json.loads(args.values.read_text(encoding="utf-8"))
    print(fill(args.template, data, args.output).resolve())


if __name__ == "__main__":
    main()
