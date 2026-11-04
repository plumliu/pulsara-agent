"""Natural-footage visual QA using production overview and real vision.

Questions/checkpoints come from independently inspected source frames. Source
continuations are fully drained using the production selection/render pipeline;
only those pictures reach the sealed-tool vision probe. A separate native-tool
run verifies the full Host path. No alternate layout/selector is injected.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from time import monotonic

from psycopg.conninfo import make_conninfo

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.desktop_runtime import validate_managed_postgres
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import (
    LLMTextPart,
    LLMImagePart,
    LLMMessage,
    FrozenPromptContent,
    PromptContent,
)
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.media.frames import FRAME_ANALYSIS_FRAMES, FrameBudget
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
from tests.dogfood.run_tool_output_pagination_dogfood import _trace
from tests.dogfood.run_media_visual_benchmark import collect_overview_pages
from tests.support.postgres_database import (
    admin_root_dsn,
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
)

from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.ephemeral import (
    PreparedEphemeralRequest,
    execute_ephemeral_request,
)
from pulsara_agent.llm.frozen_target import _freeze_provider_physical_call_target
from pulsara_agent.llm.provider_open import EphemeralProbeCredentialOwner
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import resolve_model_call, with_call_output_cap
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.primitives.model_call import ModelCallPurpose
from tests.dogfood.run_ephemeral_products_dogfood import _RecordedBorrow


class RecordingProbeRuntime(ModelRuntime):
    def borrow_transport(self, permit):
        return _RecordedBorrow(super().borrow_transport(permit), self.records)


async def evaluate_pictures(runtime, saved, selected, prompt, images):
    # User-requested functional connection probes use the existing ephemeral
    # permit/adapter with a truly empty tool surface and no dispatcher/Host.
    owner = EphemeralProbeCredentialOwner(
        selected,
        saved.require_model_api_key(selected.id) if selected.requires_api_key else "",
    )
    probe = RecordingProbeRuntime(
        settings=owner,
        catalog=runtime.catalog,
        route_wires=runtime.route_wires,
        retry=LLMRetryConfig(enabled=False, attempts=1),
    )
    probe.records = []
    timeout = OpenAITransportTimeoutPolicy(10, 30, 10, 120, 120)
    binding = _binding(runtime, selected)
    target = with_call_output_cap(
        probe.resolve_target(binding, timeout_policy=timeout), 1024
    )
    call = resolve_model_call(
        target=target, binding=binding, purpose=ModelCallPurpose.CONNECTION_PROBE
    )
    context = LLMContext(
        messages=(
            LLMMessage.user_content(
                FrozenPromptContent(
                    (
                        LLMTextPart(prompt),
                        *(
                            LLMImagePart(i.mime, i.data, i.width, i.height)
                            for i in images
                        ),
                    )
                )
            ),
        ),
        system_prompt="Inspect supplied pictures only. Return the requested JSON. If a fact is unreadable or unseen, use null. No tools are available.",
        context_id=random_id("context"),
        resolved_model_call_id=call.resolved_model_call_id,
        model_call_index=None,
    )
    prepared = PreparedEphemeralRequest(
        call_target=_freeze_provider_physical_call_target(
            target=target,
            call=call,
            maximum_input_tokens=target.context_budget.input_budget_tokens,
        ),
        context=context,
        timeout_policy=timeout,
        credential_owner=owner,
    )
    try:
        result = await execute_ephemeral_request(runtime=probe, prepared=prepared)
        assert len(probe.records) == 1
        payload = probe.records[0]["payload"]
        assert not payload.get("tools") and "data:image/" in json.dumps(payload)
        return result, probe.records
    finally:
        owner.close()
        assert not probe._active_borrows


ROOT = Path(__file__).resolve().parents[2]
BUDGET = FrameBudget(4_000_000, 1280 * 720, 1, FRAME_ANALYSIS_FRAMES)
CASES = {
    "screencast": {
        "path": "assets/Pulsara-demo.mp4",
        "from": 0,
        "to": 54.2,
        "attribution": "Pulsara repository demonstration asset, assets/Pulsara-demo.mp4; no external copy or new license asserted.",
        "checkpoint_truth": {
            "source_t": 50.0,
            "title": "卖得更多，赚得更少",
            "paid_amount": 49.3,
            "profit_amount": 1.9,
            "report_side": "right",
        },
        "question": "Return JSON: app_name (visible app title), report_title (late right-side report headline, exact Chinese), paid_amount (late report 实付金额 in 万), profit_amount (late report 贡献利润 in 万), report_side (left/right), exact_spoken_words (null: pictures alone cannot establish speech). Use null for unreadable or unseen facts.",
        "native_prompt": "Use read_video_frames on assets/Pulsara-demo.mp4 from 49 to 51 mode detail with crop [0.6,0.1,0.85,0.9]. Read the report headline and 实付金额. Do not use other tools.",
    },
    "lecture": {
        "path": "output/asr-test-media/long/video/06_zh_technical_long.mp4",
        "from": 0,
        "to": 100,
        "attribution": "Wikidata Taiwan, COSCUP 2022 OpenStreetMap x Wikidata 09; CC BY 4.0; local H.264/AAC derivative of official 480p source, sampled frame derivatives retain attribution.",
        "source_url": "https://commons.wikimedia.org/wiki/File:COSCUP_2022_OpenStreetMap_x_Wikidata_09.webm",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "checkpoint_truth": {
            "at_9.9993489583": "Building a Double Linked Map Information Utility",
            "at_24.9993489583": "whoami (Ivan)",
            "at_54.9993489583": "Stories is all around",
        },
        "question": "Return JSON: talk_title (English title slide), intro_name (name in whoami slide), later_slide_title (magazine/collage slide heading), speaker_side (left/right relative to projection), intro_t (one supplied timestamp showing whoami), exact_spoken_words (null: sampled pictures cannot establish speech). Use null if not in evidence or unreadable.",
        "native_prompt": "Use read_video_frames on output/asr-test-media/long/video/06_zh_technical_long.mp4 from 24 to 26 mode detail. Read the heading of the self-introduction slide. Do not use other tools.",
    },
    "narrated": {
        "path": "output/asr-test-media/video/04_en_speech.mp4",
        "from": 3,
        "to": 40,
        "attribution": "Bogreudell, WIKITONGUES Mckensie speaking English; CC BY-SA 4.0; local H.264/AAC derivative of Wikimedia source. Sampled visual derivatives retain this attribution/license.",
        "source_url": "https://commons.wikimedia.org/wiki/File:WIKITONGUES-_Mckensie_speaking_English.webm",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "checkpoint_truth": {
            "source_t": 4.963,
            "people": 1,
            "top_color": "yellow/gold/mustard",
            "necklace": True,
            "exact_spoken_words": None,
        },
        "question": "Return JSON: people (number of visible people), top_color (color of the main person's top), necklace (boolean for visible necklace), exact_spoken_words (null unless actual speech evidence is provided; pictures alone cannot establish the words). Do not infer a person's identity. Use null if unknown.",
        "native_prompt": "Use read_video_frames on output/asr-test-media/video/04_en_speech.mp4 from 4 to 6 mode detail. State how many people are shown, top color and whether a necklace is visible. No identity or speech inference; do not use other tools.",
    },
}


def prepare(workspace, category, case):
    begin = monotonic()
    with MediaSource.open((ROOT / case["path"]).resolve()) as source:
        info = probe_media(source)
        images, index, pages = collect_overview_pages(
            source, info, start=case["from"], end=case["to"], budget=BUDGET
        )
    elapsed = monotonic() - begin
    paths = []
    for i, image in enumerate(images):
        path = workspace / f"{category}-production_overview-{i + 1}.jpg"
        path.write_bytes(image.data)
        paths.append(str(path))
    return images, index, pages, paths, elapsed


def decoded(answer):
    try:
        return json.loads(answer[answer.index("{") : answer.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None


def score(category, answer, index):
    value = decoded(answer)
    if not isinstance(value, dict):
        return {
            "parse_error": True,
            "facts_total": {"screencast": 5, "lecture": 4, "narrated": 3}[category],
            "facts_correct": 0,
            "unsupported_speech_claim": None,
        }

    def norm(x):
        return (
            "".join(str(x or "").lower().split())
            .replace("，", "")
            .replace(",", "")
            .replace("(", "")
            .replace(")", "")
        )

    if category == "screencast":
        correct = [
            norm(value.get("app_name")) == "pulsara",
            norm(value.get("report_title")) == "卖得更多赚得更少",
            value.get("paid_amount") == 49.3,
            value.get("profit_amount") == 1.9,
            value.get("report_side") == "right",
        ]
    elif category == "lecture":
        correct = [
            norm(value.get("talk_title"))
            == norm("Building a Double Linked Map Information Utility"),
            "ivan" in norm(value.get("intro_name")),
            norm(value.get("later_slide_title")) == norm("Stories is all around"),
            value.get("speaker_side") == "left",
        ]
    else:
        correct = [
            value.get("people") == 1,
            any(
                c in norm(value.get("top_color")) for c in ("yellow", "gold", "mustard")
            ),
            value.get("necklace") is True,
        ]
    return {
        "facts_correct": sum(correct),
        "facts_total": len(correct),
        "fact_results": correct,
        "unsupported_speech_claim": value.get("exact_spoken_words") is not None,
        "parsed": value,
        "located_intro": category == "lecture"
        and type(value.get("intro_t")) in (int, float)
        and 24 <= value["intro_t"] < 55
        and any(abs(r["t"] - value["intro_t"]) < 0.001 for r in index),
    }


async def run(args):
    home = require_pulsara_home()
    original_home = os.environ.get("PULSARA_HOME")
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
    workspace = (args.output.parent / "natural-benchmark-images").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    isolated = workspace / "home"
    isolated.mkdir(exist_ok=True)
    records = []
    core = None
    report = {
        "latency_definition": "Preparation includes source open/probe, production sampling/layout/encoding; end_to_end adds the complete model call. Shared catalog/runtime startup and artifact file writes are excluded. One run per source; no population latency claim.",
        "output_budget": "Each fixed-picture probe uses the same 1024 total output-token allocation, including model reasoning. A response hitting this boundary is retained as incomplete and not rerun.",
        "model": args.model,
        "pipeline": "production_overview",
        "scope": "Natural screencast, filmed lecture, stable narrated portrait; one production-overview sealed-tools vision probe per source. Natural action/narrative not evaluated.",
        "sources": CASES,
        "budget": {
            "maximum_images": 1,
            "image_pixels": BUDGET.image_pixels,
            "scope": "per accepted source page, not the combined picture probe",
            "analysis_frames": BUDGET.analysis_frames,
        },
        "runs": [],
        "native_tool_runs": [],
        "excluded_host_trial": "natural-visual-host-observation.json records normal Host investigation using additional local image/terminal evidence, interrupted before user clarification. It is distinguished by evidence source, not treated as product failure or restarted. All fixed-picture probes below have tools omitted and no dispatcher.",
        "method": "Production read_video_frames prepares the fixed overview and every accepted page is collected by copying its complete next call. No selector or layout substitution. The picture probe bundles those pages; native Host tools are separately verified. Ground-truth checkpoint images were inspected independently.",
    }

    def save():
        args.output.write_text(
            json.dumps(
                saved.credential_scrub_set().scrub_json(report),
                indent=2,
                ensure_ascii=False,
            )
        )

    async def session_for():
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=ROOT,
                trust_workspace_mcp_config=False,
            ),
            system_prompt="Answer literally using the supplied visual evidence. Do not infer speech from pictures; follow explicitly requested native tool calls.",
        )
        await session.attach_controller(random_id("attachment"))
        await session.update_model_call_binding(_binding(runtime, selected))
        return session

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
        for category, case in CASES.items():
            images, index, pages, paths, preparation = await asyncio.to_thread(
                prepare, workspace, category, case
            )
            prompt = (
                case["question"]
                + " Do not use tools for this question; inspect only the supplied sampled pictures. Pages can be in coverage-priority order; use actual source timestamps for temporal comparisons. Printed frame numbers are local to source_page. Frame indices: "
                + json.dumps(index)
            )
            begin = monotonic()
            outcome, calls = await evaluate_pictures(
                runtime, saved, selected, prompt, images
            )
            host_seconds = monotonic() - begin
            row = {
                "category": category,
                "prompt": prompt,
                "answer": outcome.text,
                "status": outcome.status.value,
                "tools_advertised": False,
                "tool_execution_owner_present": False,
                "index": index,
                "images": paths,
                "source_pages": pages,
                "preparation_seconds": preparation,
                "host_seconds": host_seconds,
                "end_to_end_seconds": preparation + host_seconds,
                "model_calls": len(calls),
                "usage": [r["terminal"] for r in calls],
                "image_pixels": sum(i.width * i.height for i in images),
                **score(category, outcome.text, index),
            }
            report["runs"].append(row)
            save()
            print(
                category,
                row["facts_correct"],
                row.get("facts_total"),
                round(row["end_to_end_seconds"], 2),
                flush=True,
            )
            session = await session_for()
            offset = len(records)
            begin = monotonic()
            try:
                outcome = await session.run_turn(
                    PromptContent.text(case["native_prompt"]),
                    command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
                trace, _ = await asyncio.to_thread(_trace, session)
                calls = [
                    r for r in records[offset:] if r["purpose"] == "agent_model_loop"
                ]
                report["native_tool_runs"].append(
                    {
                        "category": category,
                        "prompt": case["native_prompt"],
                        "answer": outcome.final_text,
                        "end_to_end_seconds": monotonic() - begin,
                        "tools": [
                            {
                                "tool": r["tool_name"],
                                "arguments": r["arguments"],
                                "state": r["result_state"],
                            }
                            for r in trace
                        ],
                        "model_calls": len(calls),
                        "usage": [r["terminal"] for r in calls],
                    }
                )
                assert any(
                    r["tool_name"] == "read_video_frames"
                    and r["result_state"] == "SUCCESS"
                    for r in trace
                )
                save()
            finally:
                await core.close_session(session.host_session_id)
        rows = report["runs"]
        usage = [u for r in rows for u in r["usage"]]
        report["summary"] = {
            "facts_correct": sum(r["facts_correct"] for r in rows),
            "facts_total": sum(
                {"screencast": 5, "lecture": 4, "narrated": 3}[r["category"]]
                for r in rows
            ),
            "incomplete_samples": sum(r["status"] != "COMPLETED" for r in rows),
            "unavailable_speech_checks": sum(
                r.get("unsupported_speech_claim") is None for r in rows
            ),
            "unsupported_speech_claims": sum(
                bool(r["unsupported_speech_claim"]) for r in rows
            ),
            "input_tokens": sum(u.get("input_tokens") or 0 for u in usage),
            "output_tokens": sum(u.get("output_tokens") or 0 for u in usage),
            "model_calls": sum(r["model_calls"] for r in rows),
            "preparation_seconds": sum(r["preparation_seconds"] for r in rows),
            "end_to_end_seconds": sum(r["end_to_end_seconds"] for r in rows),
        }
        report["status"] = (
            "completed_with_incomplete_samples"
            if any(r["status"] != "COMPLETED" for r in report["runs"])
            else "completed"
        )
        save()
    except BaseException as exc:
        report["error"] = saved.credential_scrub_set().scrub_text(str(exc))
        save()
        raise
    finally:
        report["native_observed_usage"] = [
            r["terminal"] for r in records if r.get("terminal") is not None
        ]
        save()
        if core is not None:
            await core.shutdown()
        if original_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = original_home
        await asyncio.to_thread(
            drop_postgres_test_database, admin_root_dsn(), database.database_name
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3.8-flash")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args))
