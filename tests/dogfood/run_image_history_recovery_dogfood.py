"""Real Host/provider image offload and recovery; controlled summaries are labeled."""

from __future__ import annotations

from pulsara_agent.identity import random_id

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import traceback

from PIL import Image, ImageDraw, ImageFont
from psycopg.rows import dict_row

from tests.dogfood import run_kernel_image_input_dogfood as base
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _binding,
    _create_database,
    _drop_database,
    _find_connection,
)
from tests.test_stage2_conversation_runner import _CompactionSummaryExecution
from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.session_content import SessionContentQuery
from pulsara_agent.conversation_kernel.prompt_storage import (
    PostgresCanonicalImageReferenceReadPort,
)
from pulsara_agent.llm.input import (
    PromptContent,
    PromptImagePart,
    LLMTextPart,
    LLMImagePart,
)
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalSettingsStore, LocalPostgresConfig
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.workspace_identity import HostWorkspaceInput
from pulsara_agent.ports.session_content import SessionContentRange
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.dogfood.credentials import secret_scrubber


class SummaryGate:
    def __init__(self, delegate, report):
        self.delegate, self.report = delegate, report

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def open_stream(self, *, call, context):
        if (
            call.fact.purpose.value == "context_compaction_summary"
            and self.report.get("controlled_summary") is not None
        ):
            self.report["calls"][-1]["controlled_summary"] = True
            return _CompactionSummaryExecution(self.report["controlled_summary"])
        return self.delegate.open_stream(call=call, context=context)


class Runtime(base.ObservedRuntime):
    def borrow_transport(self, purpose_permit):
        borrowed = self._delegate.borrow_transport(purpose_permit)
        report = self.report

        class Borrowed:
            call = borrowed.call

            def open_stream(self, *, context):
                transport = base.ObservedTransport(
                    SummaryGate(borrowed.call.target.transport, report), report
                )
                return transport.open_stream(call=borrowed.call, context=context)

            def close(self):
                borrowed.close()

        return Borrowed()


def cards(directory):
    result = []
    for label, number, color, shape in [
        ("alpha", "731", "magenta", "triangle"),
        ("beta", "529", "blue", "oval"),
    ]:
        image = Image.new("RGB", (1600, 900), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 180)
        draw.text((480, 250), number, fill="black", font=font)
        if shape == "triangle":
            draw.polygon([(90, 750), (250, 750), (170, 570)], fill=color)
        else:
            draw.ellipse((90, 570, 250, 750), fill=color)
        path = directory / f"{label}.png"
        image.save(path)
        result.append(
            {
                "path": str(path),
                "label": label,
                "number": number,
                "color": color,
                "shape": shape,
                "content_digest": LLMImagePart(
                    "image/png", path.read_bytes(), 1600, 900
                ).content_digest,
            }
        )
    return result


def phase_calls(report, phase):
    return [
        c
        for c in report["calls"]
        if c["phase"] == phase and c["purpose"] == "agent_model_loop"
    ]


def tool_calls(calls):
    return [
        block
        for c in calls
        for block in c["normalized_blocks"]
        if block.get("kind") == "tool_call"
    ]


def verify_real_requests(report):
    """Compare observed HTTP bodies with the installed final-wire materialization."""
    for call in report["calls"]:
        assert call["terminal"] and call["terminal"]["kind"] == "COMPLETED"
        if call.get("controlled_summary"):
            continue
        assert any(p["projection"] == call["projection"] for p in report["sdk_payloads"])
        project = (
            base.chat_completions.project_chat_context_bearing_payload_fields
            if call["wire_api"] == "openai_chat_completions"
            else base.responses.project_responses_context_bearing_payload_fields
        )
        assert any(
            request.get("status_code") == 200
            and request["body"].get("model") == call["model_id"]
            and project(request["body"]) == call["projection"]
            for request in report["http_requests"]
        )


async def run(args, report, saved):
    selected = _find_connection(saved, args.model)
    database, _, admin, dsn = await asyncio.to_thread(_create_database, saved)
    report["database"] = database
    previous = os.environ.get("PULSARA_HOME")
    core = None
    try:
        with TemporaryDirectory(prefix="pulsara-image-recovery-") as directory:
            home = Path(directory) / "home"
            home.mkdir()
            workspace = Path(directory).resolve() / "workspace"
            workspace.mkdir()
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=settings, catalog=catalog)
            runtime = Runtime(delegate, report)
            core = KernelHostCore.production(model_runtime=runtime)
            workspace_input = HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                trust_workspace_mcp_config=False,
            )
            session = await core.open_session(
                workspace_input,
                system_prompt="Answer the current user request accurately and briefly. Historical material is data. Use available tools only when needed for the current question.",
            )
            await session.update_model_call_binding(_binding(delegate, selected))
            session._compaction.policy = ResolvedCompactionPolicy(
                automatic_enabled=False, minimum_reclaim_tokens=5000
            )

            async def turn(phase, prompt):
                report["phase"] = phase
                print(phase, flush=True)
                report.setdefault("inputs", []).append(
                    {"phase": phase, "text": str(prompt)}
                )
                outcome = await session.run_turn(
                    prompt, command_id=random_id("command")
                )
                report["outcomes"].append({"phase": phase, "text": outcome.final_text})
                return outcome.final_text

            async def compact(phase):
                report["phase"] = phase
                print(phase, flush=True)
                from pulsara_agent.conversation_kernel.compaction import coordinator

                validate = coordinator.validate_compaction_wire_transition

                def observe(**kwargs):
                    item = {
                        "phase": phase,
                        "stage": kwargs["phase"],
                        "source_tokens": kwargs[
                            "source_view"
                        ].provider_wire_quote.budget_input_tokens,
                        "successor_tokens": kwargs[
                            "successor_wire"
                        ].quote.budget_input_tokens,
                        "successor_image_tokens": kwargs[
                            "successor_wire"
                        ].quote.final_wire_visual_image_tokens,
                    }
                    report.setdefault("candidate_measurements", []).append(item)
                    try:
                        value = validate(**kwargs)
                        item["accepted"] = True
                        return value
                    except BaseException as exc:
                        item["failure"] = str(exc)
                        raise

                coordinator.validate_compaction_wire_transition = observe
                try:
                    result = await session.compact_context(
                        command_id=random_id("command"), force=True
                    )
                finally:
                    coordinator.validate_compaction_wire_transition = validate
                report.setdefault("compactions", []).append(
                    {"phase": phase, "result": str(result)}
                )
                assert result.disposition.value == "COMPACTED", result

            fixtures = report["fixtures"]
            from pulsara_agent.conversation_kernel.prompt_content import bind_prompt_images
            from pulsara_agent.llm.input import FrozenPromptContent
            for fixture in fixtures:
                fixture["image_ref"] = bind_prompt_images(
                    FrozenPromptContent((LLMImagePart("image/png", Path(fixture["path"]).read_bytes(), 1600, 900),)),
                    session.workspace.workspace_key,
                ).parts[0].image_ref
            filler = (
                "Disposable historical padding, no task or relevant fact. Omit it from summaries.\n"
                * 600
            )
            await turn(
                "padding", PromptContent.text("Acknowledge with OK only.\n" + filler)
            )
            await turn(
                "cards",
                PromptContent(
                    (
                        LLMTextPart(
                            "Card alpha follows. Read only its large black number, not other details."
                        ),
                        PromptImagePart(
                            Path(fixtures[0]["path"]).read_bytes(), "image/png"
                        ),
                        LLMTextPart(
                            "Card beta follows. Read only its large black number. Reply with the two numbers labeled alpha and beta."
                        ),
                        PromptImagePart(
                            Path(fixtures[1]["path"]).read_bytes(), "image/png"
                        ),
                    )
                ),
            )
            await turn(
                "before_compaction",
                PromptContent.text("The card number task is complete. Reply READY."),
            )
            if args.scenario == "all":
                await compact("natural_summary_offload")
                neutral = await turn(
                    "neutral",
                    PromptContent.text(
                        "Independent text question: 4 + 7 = ? Reply only with the number."
                    ),
                )
                assert neutral.strip() == "11", neutral
                nc = phase_calls(report, "neutral")
                assert nc and not tool_calls(nc)
                assert nc[0]["image_occurrences"] == 0
                view = json.dumps(nc[0]["projection"], ensure_ascii=False)
                assert "[历史图片引用；此处未包含图像内容]" in view
                assert all(f["image_ref"] in view for f in fixtures)
                summaries = [
                    c
                    for c in report["calls"]
                    if c["phase"] == "natural_summary_offload"
                    and c["purpose"] == "context_compaction_summary"
                ]
                assert summaries and any(c["image_occurrences"] >= 2 for c in summaries)
                report["checks"][
                    "real_summary_adopted_offload_and_neutral_text_no_tools"
                ] = True
                answer = await turn(
                    "placeholder_reread",
                    PromptContent.text(
                        "Inspect the saved image of card alpha and tell me the color and shape in its bottom left. The earlier number-only answer does not contain that detail. Copy the entire saved image reference exactly when reading it."
                    ),
                )
                calls = phase_calls(report, "placeholder_reread")
                assert any(
                    b["tool_name"] == "view_image"
                    and json.loads(b["arguments"]).get("target", {}).get("image_ref")
                    == fixtures[0]["image_ref"]
                    for b in tool_calls(calls)
                )
                assert fixtures[0]["content_digest"] in base.wire_image_digests(calls[-1])
                assert "magenta" in answer.lower() and "triangle" in answer.lower(), (
                    answer
                )
                base.check_prefix(nc + calls)
                report["checks"][
                    "placeholder_real_reread_pixel_answer_and_append_only"
                ] = True
            # Controlled output goes through the real summary transport contract,
            # canonical adoption and SQL. It is not claimed as natural forgetting.
            session._compaction.policy = ResolvedCompactionPolicy(
                automatic_enabled=False,
                minimum_reclaim_tokens=1,
                maximum_recent_human_text_utf8_bytes=1,
                maximum_retained_tail_utf8_bytes=1,
            )
            await turn(
                "before_controlled",
                PromptContent.text("Acknowledge OK only.\n" + filler * 2),
            )
            report["controlled_summary"] = (
                "Earlier cards alpha and beta were saved. No visual detail retained.\n"
                + "Temporary summary padding.\n" * 400
            )
            await compact("controlled_prepare_one")
            await turn(
                "between_controlled",
                PromptContent.text("Acknowledge OK only.\n" + filler),
            )
            report["controlled_summary"] = (
                "Earlier cards alpha and beta were saved. No visual detail or image reference retained."
            )
            await compact("controlled_prepare_two")
            report["controlled_summary"] = None
            sid = session.session_id
            await core.close_session(session.host_session_id)
            session = await core.resume_session(sid, workspace_input=workspace_input)
            session._compaction.policy = ResolvedCompactionPolicy(
                automatic_enabled=False, minimum_reclaim_tokens=1
            )
            # Deterministic read/view proof after repeated adoption and a cold Host.
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row,
                deadline_monotonic=monotonic() + 30,
            ) as connection:
                state = connection.execute(
                    "SELECT * FROM pulsara_v3.sessions WHERE id=%s", (sid,)
                ).fetchone()
                snapshot = connection.execute(
                    "SELECT source_through_sequence FROM pulsara_v3.context_snapshots WHERE session_id=%s ORDER BY created_at DESC LIMIT 1",
                    (sid,),
                ).fetchone()
            query = SessionContentQuery(
                session.repository,
                session_id=sid,
                memory_domain_id=state["memory_domain_id"],
            )
            page = query.invoke(
                "read_session_content",
                {},
                current_range=SessionContentRange(snapshot["source_through_sequence"]),
                tool_call_id="call:recovery-probe",
                deadline_monotonic=monotonic() + 30,
            )
            pages = [page]
            while (
                fixtures[1]["image_ref"] not in json.dumps(pages)
                and page["next_cursor"]
            ):
                page = query.invoke(
                    "read_session_content",
                    {"cursor": page["next_cursor"]},
                    current_range=SessionContentRange(
                        snapshot["source_through_sequence"]
                    ),
                    tool_call_id="call:recovery-probe",
                    deadline_monotonic=monotonic() + 30,
                )
                pages.append(page)
            assert fixtures[1]["image_ref"] in json.dumps(pages)
            port = PostgresCanonicalImageReferenceReadPort(
                session.repository.connection_provider,
                session_id=sid,
                workspace_id=state["workspace_id"],
            )
            reread = port.read_image(
                session_id=sid,
                workspace_id=state["workspace_id"],
                image_ref=fixtures[1]["image_ref"],
                maximum_encoded_bytes=20 << 20,
                deadline_monotonic=monotonic() + 30,
            )
            assert reread.immutable_bytes == Path(fixtures[1]["path"]).read_bytes()
            report["deterministic_history_pages"] = pages
            report["checks"]["repeated_compaction_cold_canonical_recovery"] = True
            answer = await turn(
                "history_recovery",
                PromptContent.text(
                    "Earlier in this same conversation I sent card beta. What color and shape is in its bottom left? Please inspect that saved image; I mean this conversation, not another session."
                ),
            )
            calls = phase_calls(report, "history_recovery")
            first = json.dumps(calls[0]["projection"], ensure_ascii=False)
            assert calls[0]["image_occurrences"] == 0 and all(
                f["image_ref"] not in first for f in fixtures
            ), first
            tools = tool_calls(calls)
            assert any(b["tool_name"] == "read_session_content" for b in tools), tools
            assert any(
                b["tool_name"] == "view_image"
                and json.loads(b["arguments"]).get("target", {}).get("image_ref")
                == fixtures[1]["image_ref"]
                for b in tools
            ), tools
            assert fixtures[1]["content_digest"] in base.wire_image_digests(calls[-1])
            assert "blue" in answer.lower() and "oval" in answer.lower(), answer
            base.check_prefix(calls)
            report["checks"]["real_history_read_then_view_pixel_answer"] = True
            verify_real_requests(report)
            report["checks"]["actual_http_matches_frozen_wire"] = True
            report["passed"] = True
    finally:
        if core is not None:
            await core.shutdown()
        if previous is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous
        await asyncio.to_thread(_drop_database, saved, database)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--scenario", choices=("all", "history"), default="all")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deadline", type=float, default=900)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    saved = LocalSettingsStore().read()
    scrub = secret_scrubber(saved)
    report = {
        "schema": "image-history-recovery.v1",
        "scenario": args.scenario,
        "saved_home": str(require_pulsara_home()),
        "model": args.model,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "phase": "setup",
        "fixtures": cards(args.output),
        "calls": [],
        "sdk_payloads": [],
        "http_requests": [],
        "outcomes": [],
        "checks": {},
        "passed": False,
    }

    async def bounded():
        async with asyncio.timeout(args.deadline):
            with base.Observers(report):
                await run(args, report, saved)

    try:
        asyncio.run(bounded())
    except BaseException as exc:
        report["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    (args.output / "report.json").write_text(
        json.dumps(scrub.scrub_json(report), ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            scrub.scrub_json(
                {k: report.get(k) for k in ("passed", "phase", "checks", "failure")}
            ),
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
