"""Paired, real-provider visual benchmark on small synthetic ground truth.

Selection pair: uniform/grid vs production anchor+changes/grid. Layout pair:
identical production-selected frames/grid vs production DP. Fixed frame-count,
image and pixel admission, same source/questions/model; actual usage is measured.
This narrow fixture suite cannot establish general visual understanding quality.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from fractions import Fraction
import io
import json
import math
import os
from pathlib import Path
from time import monotonic

import av
from PIL import Image, ImageDraw, ImageFont
from psycopg.conninfo import make_conninfo

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.desktop_runtime import validate_managed_postgres
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import LLMTextPart, PromptContent, PromptImagePart
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.media.frames import (
    FrameBudget,
    FrameImage,
    _visible,
    read_video_frames,
)
from pulsara_agent.media.frames_selection import Sample, uniform_indices
from pulsara_agent.media.source import MediaSource, probe_media
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _find_connection,
)
from tests.support.postgres_database import (
    admin_root_dsn,
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
)

CASES = {
    "lecture": ("STEP ONE", "STEP TWO", "CAUTION"),
    "screencast": ("CLOSED", "SAVED", "ERROR 417"),
    "action": ("START", "FINISH", "JUMP"),
    "narrative": ("ARRIVAL", "DEPARTURE", "VISITOR"),
}
BUDGET = FrameBudget(4_000_000, 1280 * 720, 1, 128, maximum_frames=6)


def fixture(path, category, truth):
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 24)
    small = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 16)
    frames = []
    for i in range(120):
        transient = 5 <= i < 9
        final = i >= 60
        image = Image.new("RGB", (320, 180), "#fff9dd" if transient else "#e8edf4")
        d = ImageDraw.Draw(image)
        d.text((12, 12), category.upper(), fill="black", font=small)
        label = truth[2] if transient else truth[1] if final else truth[0]
        if category == "action":
            x = 260 if final else 160 if transient else 50
            y = 75 if transient else 125
            d.ellipse(
                (x - 22, y - 22, x + 22, y + 22), fill="orange" if transient else "blue"
            )
            d.line((15, 149, 305, 149), fill="black", width=3)
        elif category == "lecture":
            d.text((30, 110), "x + 2" if final else "x + 1", fill="navy", font=font)
        elif category == "screencast":
            d.rectangle((25, 105, 280, 150), outline="navy", width=3)
            d.text((40, 112), "SAVE", fill="navy", font=font)
        else:
            d.rectangle(
                (215, 82, 280, 168),
                fill="red" if transient else "green" if final else "blue",
            )
            d.ellipse((110, 90, 135, 115), fill="black")
            d.line((122, 115, 122, 155), fill="black", width=4)
        d.text((20, 48), label, fill="red" if transient else "black", font=font)
        frames.append(image)
    with av.open(str(path), "w") as output:
        stream = output.add_stream("ffv1", rate=10)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "bgr0"
        stream.time_base = Fraction(1, 10)
        for i, image in enumerate(frames):
            frame = av.VideoFrame.from_image(image)
            frame.pts, frame.time_base = i, Fraction(1, 10)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


def grid(source, info, times):
    columns = math.ceil(math.sqrt(len(times)))
    rows = math.ceil(len(times) / columns)
    w = min(1280 // columns, int(math.sqrt(BUDGET.image_pixels / len(times) * 16 / 9)))
    h = math.floor(w * 9 / 16)
    canvas = Image.new("RGB", (columns * w, rows * h), "#202020")
    index = []
    for i, t in enumerate(times):
        actual, _, image = _visible(source, info, t, None)
        x, y = i % columns * w, i // columns * h
        canvas.paste(image.resize((w, h), Image.Resampling.LANCZOS), (x, y))
        d = ImageDraw.Draw(canvas)
        box = d.textbbox((x + 4, y + 3), str(i + 1))
        d.rectangle((x, y, box[2] + 5, box[3] + 4), fill="black")
        d.text((x + 4, y + 3), str(i + 1), fill="white")
        index.append(
            {
                "n": i + 1,
                "image": 1,
                "t": actual,
                "cell_rect": [
                    x / canvas.width,
                    y / canvas.height,
                    (x + w) / canvas.width,
                    (y + h) / canvas.height,
                ],
            }
        )
    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=90)
    return (
        FrameImage(output.getvalue(), "image/jpeg", canvas.width, canvas.height),
    ), index


def prepare(workspace, category, truth):
    source_path = workspace / f"{category}.mkv"
    fixture(source_path, category, truth)
    begin = monotonic()
    with MediaSource.open(source_path) as source:
        info = probe_media(source)
        production = read_video_frames(
            source, info, start=0, end=info.duration, mode="overview", budget=BUDGET
        )
        body = json.loads(production.text)
        times = [row["t"] for row in body["frames"]]
        # The uniform baseline uses the exact same real source PTS pool and count.
        samples = [Sample(i / 10, None, 0) for i in range(120)]
        uniform = [samples[i].t for i in uniform_indices(samples, len(times))]
        anchor_grid = grid(source, info, times)
        uniform_grid = grid(source, info, uniform)
    preparation_seconds = monotonic() - begin
    variants = {
        "uniform_grid": uniform_grid,
        "anchor_changes_grid": anchor_grid,
        "anchor_changes_dp": (production.images, body["frames"]),
    }
    result = []
    for name, (images, index) in variants.items():
        paths = []
        for i, image in enumerate(images):
            p = workspace / f"{category}-{name}-{i + 1}.jpg"
            p.write_bytes(image.data)
            paths.append(str(p))
        result.append((name, images, index, paths, preparation_seconds))
    return result


def parse(answer):
    try:
        return json.loads(answer[answer.index("{") : answer.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None


def grade(answer, truth, index):
    value = parse(answer)
    if not isinstance(value, dict):
        return {
            "parse_error": True,
            "facts_correct": 0,
            "facts_total": 3,
            "localized": False,
            "unsupported_claims": None,
            "event_selected": any(0.5 <= r["t"] < 0.9 for r in index),
        }
    facts = sum(
        isinstance(value.get(k), str) and value[k].strip().upper() == expected
        for k, expected in zip(("first_label", "last_label", "brief_label"), truth)
    )
    time = value.get("brief_t")
    localized = type(time) in (int, float) and 0.5 <= time < 0.9
    event_selected = any(0.5 <= r["t"] < 0.9 for r in index)
    unsupported = int(value.get("brand") is not None) + int(
        not event_selected and value.get("brief_label") is not None
    )
    return {
        "facts_correct": facts,
        "facts_total": 3,
        "localized": localized,
        "unsupported_claims": unsupported,
        "event_selected": event_selected,
        "parsed": value,
    }


async def run(args, report):
    home = require_pulsara_home()
    previous_home = os.environ.get("PULSARA_HOME")
    socket = str(home / "desktop" / "socket")

    def dsn(role):
        return make_conninfo(host=socket, port="5432", dbname="pulsara", user=role)

    postgres = LocalPostgresConfig(dsn("pulsara_runtime"), dsn("pulsara_admin"))
    validate_managed_postgres(postgres)
    saved = LocalSettingsStore(home / "local-settings.yaml", postgres=postgres).read()
    selected = _find_connection(saved, args.model)
    os.environ.update(
        PULSARA_POSTGRES_DSN=postgres.runtime_dsn,
        PULSARA_POSTGRES_ADMIN_DSN=postgres.admin_dsn,
    )
    database = await asyncio.to_thread(create_migrated_postgres_test_database)
    workspace = args.output.parent.resolve() / "visual-benchmark-fixtures"
    workspace.mkdir(parents=True, exist_ok=True)
    isolated = workspace / "home"
    isolated.mkdir(exist_ok=True)
    records = []
    core = None
    report.update(
        model=args.model,
        scope="synthetic paired 4-category benchmark; one run per variant; no broad accuracy or latency guarantee",
        declared_budget={
            "maximum_images": BUDGET.maximum_images,
            "image_pixels": BUDGET.image_pixels,
            "maximum_frames": BUDGET.maximum_frames,
            "analysis_frames": BUDGET.analysis_frames,
        },
        runs=[],
    )

    def save():
        args.output.write_text(
            json.dumps(saved.credential_scrub_set().scrub_json(report), indent=2)
        )

    try:
        os.environ["PULSARA_HOME"] = str(isolated)
        catalog = ModelCatalogOwner(ModelsDevCatalogClient())
        await catalog.refresh()
        runtime = ModelRuntime.production(
            settings=_ReadOnlySettingsStore(
                replace(
                    saved,
                    postgres=LocalPostgresConfig(
                        database.runtime_dsn, database.admin_dsn
                    ),
                )
            ),
            catalog=catalog,
        )
        core = KernelHostCore.production(
            model_runtime=_RecordingModelRuntime(runtime, records)
        )
        for category, truth in CASES.items():
            variants = await asyncio.to_thread(prepare, workspace, category, truth)
            for name, images, index, paths, preparation_seconds in variants:
                prompt = (
                    "Inspect only these sampled frames and their actual source timestamps. Do not use any tools. "
                    "These are sparse observations; do not invent unseen events or interpolate. "
                    "Return only JSON with keys first_label, last_label, brief_label, brief_t, brand. "
                    "Read the main large label in first/last supplied frames. brief_label is the visible temporary main label that differs from both; "
                    "brief_t is its supplied timestamp; use null if absent. brand is the explicit company/brand name shown; null if absent. "
                    "Frame index: " + json.dumps(index)
                )
                session = await core.open_session(
                    HostWorkspaceInput(
                        workspace_kind="project",
                        workspace_root=workspace,
                        trust_workspace_mcp_config=False,
                    ),
                    system_prompt="Answer the user from supplied visual evidence. Be literal and do not use tools.",
                )
                try:
                    await session.attach_controller(random_id("attachment"))
                    await session.update_model_call_binding(_binding(runtime, selected))
                    start_record = len(records)
                    begin = monotonic()
                    result = await session.run_turn(
                        PromptContent(
                            (
                                LLMTextPart(prompt),
                                *(PromptImagePart(i.data, i.mime) for i in images),
                            )
                        ),
                        command_id=random_id("command"),
                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                    )
                    elapsed = monotonic() - begin
                    calls = [
                        r
                        for r in records[start_record:]
                        if r["purpose"] == "agent_model_loop"
                    ]
                    assert calls and any(
                        "data:image/" in json.dumps(r["provider_input"]) for r in calls
                    )
                    row = {
                        "category": category,
                        "variant": name,
                        "truth": truth,
                        "index": index,
                        "images": paths,
                        "image_pixels": sum(i.width * i.height for i in images),
                        "image_bytes": sum(len(i.data) for i in images),
                        "prompt": prompt,
                        "answer": result.final_text,
                        "seconds": elapsed,
                        "shared_preparation_seconds": preparation_seconds,
                        "latency_definition": "Host run_turn seconds excludes shared fixture/selection/layout preparation; shared preparation separately measured.",
                        "model_calls": len(calls),
                        "usage": [r["terminal"] for r in calls],
                        **grade(result.final_text, truth, index),
                    }
                    report["runs"].append(row)
                    save()
                    print(
                        category,
                        name,
                        row["facts_correct"],
                        "/3",
                        "event",
                        row["event_selected"],
                        "seconds",
                        round(elapsed, 2),
                        flush=True,
                    )
                finally:
                    await core.close_session(session.host_session_id)
        report["summary"] = {}
        for name in ("uniform_grid", "anchor_changes_grid", "anchor_changes_dp"):
            runs = [r for r in report["runs"] if r["variant"] == name]
            usage = [u for r in runs for u in r["usage"]]
            report["summary"][name] = {
                "facts_correct": sum(r["facts_correct"] for r in runs),
                "facts_total": 12,
                "localized_events": sum(r["localized"] for r in runs),
                "selection_misses": sum(not r["event_selected"] for r in runs),
                "unsupported_claims": sum(r["unsupported_claims"] or 0 for r in runs),
                "model_calls": sum(r["model_calls"] for r in runs),
                "input_tokens": sum(u.get("input_tokens") or 0 for u in usage),
                "output_tokens": sum(u.get("output_tokens") or 0 for u in usage),
                "total_seconds": sum(r["seconds"] for r in runs),
            }
        report["unmeasured"] = [
            "natural lecture/screencast/action/narrative videos",
            "multiple repeats or confidence intervals",
            "ASR cold/hot/parallel/quality/cost (saved ASR config absent)",
            "subtitle disagreement covered by separate production Host dogfood",
            "multi-window quiet and active user steer measured separately by run_media_visual_behavior.py",
        ]
        save()
    except Exception as exc:
        report["error"] = saved.credential_scrub_set().scrub_text(str(exc))
        save()
        raise
    finally:
        if core is not None:
            await core.shutdown()
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        await asyncio.to_thread(
            drop_postgres_test_database, admin_root_dsn(), database.database_name
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3.8-flash")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args, {}))
