"""Serve a disposable real-provider Web Host for file-import browser dogfood.

Read saved settings before switching homes. The browser drives normal UI; touch
OUTPUT/stop to finish and collect actual provider bodies plus kernel tool traces.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from zipfile import ZipFile

from PIL import Image, ImageDraw

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalSettingsStore, LocalPostgresConfig
from pulsara_agent.tool_permission import default_permission_policy
from pulsara_agent.web_app.application import LocalWebApplication
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _create_database,
    _drop_database,
    _find_connection,
    _binding,
    _scrub,
)
from tests.dogfood.run_kernel_image_input_dogfood import Observers, ObservedRuntime
from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace
from tests.dogfood.run_pr03_user_control_dogfood import _rows


def fixtures(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    # A small PDF with one text stream, sufficient for byte/header inspection.
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = b"BT /F1 18 Tf 20 100 Td (PDF_IMPORT_7319) Tj ET"
    objects.append(
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream"
    )
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    (root / "方案.pdf").write_bytes(pdf)
    with ZipFile(root / "草稿.docx", "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DOCX_IMPORT_8642</w:t></w:r></w:p></w:body></w:document>',
        )
    with Image.new("RGB", (320, 180), "white") as image:
        ImageDraw.Draw(image).rectangle((60, 30, 260, 150), fill="red")
        image.save(root / "red-card.png")
    folder = root / "资料目录"
    (folder / "nested").mkdir(parents=True, exist_ok=True)
    (folder / "nested" / "marker.txt").write_text("DIRECTORY_IMPORT_5927\n")
    (folder / "red-card.png").write_bytes((root / "red-card.png").read_bytes())
    (folder / "empty.txt").touch()


async def run(output: Path, model: str):
    saved = LocalSettingsStore().read()
    saved_home = str(require_pulsara_home())
    connection = _find_connection(saved, model)
    database, _, admin, runtime_dsn = _create_database(saved)
    output.mkdir(parents=True, exist_ok=True)
    root = output.resolve()
    fixtures(root / "fixtures")
    (root / "workspace").mkdir(exist_ok=True)
    old_home = os.environ.get("PULSARA_HOME")
    report = {
        "phase": "file-path-import",
        "calls": [],
        "sdk_payloads": [],
        "http_requests": [],
        "saved_home": saved_home,
        "database": database,
        "runtime_dsn": runtime_dsn,
        "model": model,
    }
    app = None
    try:
        os.environ["PULSARA_HOME"] = str(root / "home")
        settings = _ReadOnlySettingsStore(
            replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
        )
        catalog = ModelCatalogOwner(ModelsDevCatalogClient())
        await catalog.refresh()
        delegate = ModelRuntime.production(settings=settings, catalog=catalog)
        runtime = ObservedRuntime(delegate, report)
        app = LocalWebApplication(
            settings=settings,
            trust_workspace_mcp_config=False,
            permission_policy=default_permission_policy(),
            catalog=catalog,
            model_runtime=runtime,
        )
        with Observers(report):
            await app.start()
            handle = await app.sessions.create_session(
                workspace_kind="project", workspace_path=str(root / "workspace")
            )
            session = app.sessions.session_by_host_id(handle.host_session_id)
            await session.update_model_call_binding(_binding(delegate, connection))
            (root / "ready.json").write_text(
                json.dumps(
                    {
                        "origin": app.origin,
                        "session_id": handle.session_id,
                        "fixtures": str(root / "fixtures"),
                    },
                    ensure_ascii=False,
                )
            )
            print(app.origin, flush=True)
            while not (root / "stop").exists():
                report["trace"] = await asyncio.to_thread(_tool_trace, session)
                report["transcript"] = await asyncio.to_thread(
                    _rows,
                    session,
                    "SELECT e.entry_sequence, e.entry_kind, convert_from(e.inline_content,'UTF8') AS content, "
                    "(SELECT string_agg(convert_from(b.inline_content,'UTF8'),'' ORDER BY b.block_ordinal) "
                    "FROM pulsara_v3.assistant_message_blocks b WHERE b.session_id=e.session_id "
                    "AND b.assistant_entry_id=e.id AND b.block_kind='TEXT') AS assistant_text "
                    "FROM pulsara_v3.transcript_entries e WHERE e.session_id=%s ORDER BY e.entry_sequence",
                    (session.session_id,),
                )
                (root / "evidence.json").write_text(
                    json.dumps(
                        _scrub(report, tuple(x.value for x in saved.model_api_keys)),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                await asyncio.sleep(1)
    finally:
        if app is not None:
            await app.aclose()
        if old_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = old_home
        _drop_database(saved, database)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="openai/gpt-6-luna")
    args = parser.parse_args()
    asyncio.run(run(args.output, args.model))


if __name__ == "__main__":
    main()
