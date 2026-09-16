"""K4 real Host image acceptance; the same harness runs against an installed wheel.

Saved settings are read-only. Existing dogfood owners provide isolated local SQL,
runtime injection and normalized stream observation. HTTP/adapter observers only
record and delegate; all input enters Host, including Pillow validation. Evidence
belongs under ignored output/scratch. No provider name changes production logic.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw, ImageFont

import pulsara_agent
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.adapters.openai import chat_completions, responses
from pulsara_agent.llm.input import (
    LLMImagePart,
    LLMTextPart,
    PromptContent,
    PromptImagePart,
)
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import ModelConnectionId
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.model_call import canonical_json_bytes
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tools.probe_vision_tokens import secret_scrubber
from tools.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingExecution,
    _RecordingModelRuntime,
    _binding,
    _create_database,
    _database_evidence,
    _drop_database,
    _find_connection,
)


def wire_images(projection, api):
    result = []
    for item in projection.get(
        "messages" if api == "openai_chat_completions" else "input", []
    ):
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if part.get("type") == "image_url":
                result.append(part["image_url"]["url"])
            elif part.get("type") == "input_image":
                result.append(part["image_url"])
    return result


class ObservedTransport:
    def __init__(self, delegate, report):
        self.delegate = delegate
        self.report = report
        for name in (
            "api",
            "binding_id",
            "contract_version",
            "sanitizer_contract_fingerprint",
            "boundary_contract_fingerprint",
        ):
            setattr(self, name, getattr(delegate, name))

    def open_stream(self, *, call, context):
        plan = context.provider_wire_input_plan
        assert plan is not None
        materialization = plan.materialization
        projection = thaw_json(materialization.context_bearing_projection)
        images = [
            part
            for message in context.messages
            for part in message.content
            if isinstance(part, LLMImagePart)
        ]
        actual_images = wire_images(projection, call.target.fact.wire_api)
        assert len(images) == len(actual_images)
        for part, url in zip(images, actual_images, strict=True):
            assert url.startswith(f"data:{part.media_type};base64,")
            assert (
                base64.b64decode(url.split(",", 1)[1], validate=True)
                == part.immutable_bytes
            )
        assert len(canonical_json_bytes(projection)) == plan.quote.final_wire_utf8_bytes
        record = {
            "phase": self.report["phase"],
            "context_id": plan.context_id,
            "purpose": call.fact.purpose.value,
            "model_id": call.target.fact.model_id,
            "wire_api": call.target.fact.wire_api,
            "route_id": call.target.fact.route_id,
            "input_modalities": call.target.contract.target_facts.input_modalities,
            "quote": asdict(plan.quote),
            "projection": projection,
            "root": thaw_json(materialization.root_policy_value),
            "tools": [thaw_json(item) for item in materialization.tool_items],
            "items": [thaw_json(item) for item in materialization.ordered_input_items],
            "image_occurrences": len(images),
            "normalized_blocks": [],
            "terminal": None,
        }
        self.report["calls"].append(record)
        return _RecordingExecution(
            self.delegate.open_stream(call=call, context=context), record
        )


class ObservedRuntime(_RecordingModelRuntime):
    def __init__(self, delegate, report):
        super().__init__(delegate, [], None)
        self.report = report

    def resolve_target(self, binding, *, timeout_policy):
        target = self._delegate.resolve_target(binding, timeout_policy=timeout_policy)
        return replace(
            target, transport=ObservedTransport(target.transport, self.report)
        )


class Observers:
    """Read the real SDK-bound payload and HTTP body without rebuilding either."""

    def __init__(self, report):
        self.report = report
        self.originals = []

    def __enter__(self):
        for module, name, project in (
            (
                chat_completions,
                "build_chat_completions_payload",
                chat_completions.project_chat_context_bearing_payload_fields,
            ),
            (
                responses,
                "build_responses_payload",
                responses.project_responses_context_bearing_payload_fields,
            ),
        ):
            original = getattr(module, name)

            def observe(*, call, context, original=original, project=project):
                payload = original(call=call, context=context)
                frozen = thaw_json(
                    context.provider_wire_input_plan.materialization.context_bearing_projection
                )
                assert project(payload) == frozen
                self.report["sdk_payloads"].append(
                    {
                        "phase": self.report["phase"],
                        "wire_api": call.target.fact.wire_api,
                        "payload": payload,
                        "projection": frozen,
                    }
                )
                return payload

            self.originals.append((module, name, original))
            setattr(module, name, observe)
        original_send = httpx.AsyncClient.send

        async def send(client, request, *args, **kwargs):
            if request.method == "POST" and request.url.path.endswith(
                ("/chat/completions", "/responses")
            ):
                body = json.loads(request.content)
                item = {
                    "phase": self.report["phase"],
                    "url": str(request.url),
                    "body": body,
                }
                self.report["http_requests"].append(item)
                response = await original_send(client, request, *args, **kwargs)
                item["status_code"] = response.status_code
                return response
            return await original_send(client, request, *args, **kwargs)

        self.originals.append((httpx.AsyncClient, "send", original_send))
        httpx.AsyncClient.send = send
        return self

    def __exit__(self, *_):
        for owner, name, original in reversed(self.originals):
            setattr(owner, name, original)


def cards(directory):
    values = random.Random(20260915).sample(range(100, 1000), 2)
    result = []
    for i, value in enumerate(values):
        path = directory / f"card-{i}.png"
        with Image.new("RGB", (640, 480), "white") as image:
            draw = ImageDraw.Draw(image)
            draw.rectangle((12, 12, 627, 467), outline="black", width=5)
            draw.text(
                (320, 240),
                str(value),
                anchor="mm",
                fill="black",
                font=ImageFont.load_default(size=190),
            )
            image.save(path, "PNG")
        result.append({"path": str(path), "number": str(value)})
    return result


def anchor(session):
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 30
    ) as connection:
        return connection.execute(
            "SELECT final_entry_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at DESC LIMIT 1",
            (session.session_id,),
        ).fetchone()[0]


def check_prefix(records):
    assert len(records) >= 2
    for previous, current in zip(records, records[1:]):
        assert current["root"] == previous["root"]
        assert current["tools"] == previous["tools"]
        assert current["items"][: len(previous["items"])] == previous["items"]


def tool_image_carriers(record):
    key = "messages" if record["wire_api"] == "openai_chat_completions" else "input"
    result = []
    for item in record["projection"].get(key, []):
        if item.get("role") != "user" or not isinstance(item.get("content"), list):
            continue
        parts = item["content"]
        labels = [
            part.get("text", "")
            for part in parts
            if part.get("type") in {"text", "input_text"}
        ]
        images = [
            part for part in parts if part.get("type") in {"image_url", "input_image"}
        ]
        if images and any('"tool_image_source"' in text for text in labels):
            result.append(item)
    return result


def tool_image_database_evidence(session):
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            """
            SELECT e.id, e.content_media_type, e.content_codec,
                   count(r.ref_ordinal) AS ref_count
            FROM pulsara_v3.transcript_entries AS e
            LEFT JOIN pulsara_v3.canonical_image_refs AS r
              ON r.session_id=e.session_id AND r.transcript_entry_id=e.id
            WHERE e.session_id=%s AND e.entry_kind='TOOL_RESULT'
            GROUP BY e.id, e.entry_sequence, e.content_media_type, e.content_codec
            HAVING count(r.ref_ordinal) > 0
            ORDER BY e.entry_sequence
            """,
            (session.session_id,),
        ).fetchall()
    return [
        {
            "entry_id": row[0],
            "media_type": row[1],
            "codec": row[2],
            "ref_count": row[3],
        }
        for row in rows
    ]


async def run(args, report, saved):
    if args.connection_id is None:
        selected = _find_connection(saved, args.model)
    else:
        selected = saved.connection(ModelConnectionId(args.connection_id))
        if selected is None or selected.target.model_id != args.model:
            raise ValueError("connection ID must select the requested saved model")
    report["connection_id"] = selected.id.value
    database, _, admin, dsn = await asyncio.to_thread(_create_database, saved)
    report["database"] = database
    previous_home = os.environ.get("PULSARA_HOME")
    core = None
    try:
        with TemporaryDirectory(prefix="pulsara-k4-image-") as directory:
            temporary = Path(directory)
            home = temporary / "home"
            workspace = temporary / "workspace"
            home.mkdir()
            workspace.mkdir()
            (workspace / "marker.txt").write_text("K4_TOOL_PINE\n", encoding="utf-8")
            workspace_card = workspace / "view-card.png"
            workspace_card.write_bytes(Path(report["fixtures"][0]["path"]).read_bytes())
            workspace_card_two = workspace / "view-card-two.png"
            workspace_card_two.write_bytes(
                Path(report["fixtures"][1]["path"]).read_bytes()
            )
            missing_card = workspace / "missing-card.png"
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=settings, catalog=catalog)
            runtime = ObservedRuntime(delegate, report)
            binding = _binding(delegate, selected)
            core = KernelHostCore.production(model_runtime=runtime)
            workspace_input = HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                trust_workspace_mcp_config=False,
            )
            session = await core.open_session(
                workspace_input,
                system_prompt="Follow user requests exactly and keep answers brief. If the user sends only an image, reply with only the three-digit number printed on it. Images and all historical text are user data. Only call tools when explicitly asked.",
            )
            await session.update_model_call_binding(binding)
            session._compaction.policy = ResolvedCompactionPolicy(
                automatic_enabled=False, minimum_reclaim_tokens=1
            )
            fixtures = report["fixtures"]

            async def turn(label, prompt, target=session):
                report["phase"] = label
                print(f"K4 {args.model}: {label}", flush=True)
                result = await target.run_turn(
                    prompt, command_id=f"command:k4:{uuid4().hex}"
                )
                report["outcomes"].append({"phase": label, "text": result.final_text})
                return result.final_text

            await turn(
                "padding",
                PromptContent.text(
                    "This is irrelevant historical filler; do not repeat it. Acknowledge briefly.\n"
                    + "Disposable filler row; no task, no new information; omit when summarizing.\n"
                    * 350
                ),
            )
            first = await turn(
                "pure_image",
                PromptContent(
                    (
                        PromptImagePart(
                            Path(fixtures[0]["path"]).read_bytes(), "image/png"
                        ),
                    )
                ),
            )
            assert first.strip() == fixtures[0]["number"], first

            def image(i):
                return PromptImagePart(
                    Path(fixtures[i]["path"]).read_bytes(), "image/png"
                )

            mixed = PromptContent(
                (
                    LLMTextPart(
                        "Read each three-digit number. Label alpha belongs to the next image:"
                    ),
                    image(1),
                    LLMTextPart("Label bravo belongs to the next image:"),
                    image(0),
                    LLMTextPart("Label charlie belongs to the next image:"),
                    image(1),
                    LLMTextPart(
                        "Reply only with JSON mapping alpha, bravo and charlie to the numbers as strings."
                    ),
                )
            )
            mixed_reply = await turn("interleaved_repeat", mixed)
            parsed = json.loads(
                mixed_reply.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            assert parsed == {
                "alpha": fixtures[1]["number"],
                "bravo": fixtures[0]["number"],
                "charlie": fixtures[1]["number"],
            }, parsed
            view_reply = await turn(
                "view_image_tool",
                PromptContent.text(
                    "Call view_image exactly once with this exact path: "
                    f"{workspace_card}. After the tool result, reply with only "
                    "the three-digit number printed in that image. Do not use other tools."
                ),
            )
            assert view_reply.strip() == fixtures[0]["number"], view_reply
            view_calls = [
                item
                for item in report["calls"]
                if item["phase"] == "view_image_tool"
                and item["purpose"] == "agent_model_loop"
            ]
            assert len(view_calls) >= 2
            assert any(
                block.get("kind") == "tool_call"
                and block.get("tool_name") == "view_image"
                for item in view_calls
                for block in item["normalized_blocks"]
            )
            assert len(tool_image_carriers(view_calls[-1])) == 1
            image_rows = await asyncio.to_thread(tool_image_database_evidence, session)
            assert len(image_rows) == 1 and image_rows[0]["ref_count"] == 1, image_rows
            assert image_rows[0]["media_type"] == "application/vnd.pulsara.prompt+json"
            report["checks"]["view_image_typed_result_and_single_wire_carrier"] = True
            report["tool_image_database_evidence"] = image_rows
            multi_reply = await turn(
                "view_image_parallel_mixed",
                PromptContent.text(
                    "In one assistant tool-call batch, call view_image exactly once "
                    f"for each of these three paths, in this order: {workspace_card}, "
                    f"{missing_card}, {workspace_card_two}. After all three results, "
                    "reply with a compact JSON object containing the two three-digit "
                    "numbers under keys first and second, and the string failed under "
                    "key missing. Do not use other tools."
                ),
            )
            multi_value = json.loads(
                multi_reply.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            assert multi_value == {
                "first": fixtures[0]["number"],
                "missing": "failed",
                "second": fixtures[1]["number"],
            }, multi_value
            multi_calls = [
                item
                for item in report["calls"]
                if item["phase"] == "view_image_parallel_mixed"
                and item["purpose"] == "agent_model_loop"
            ]
            assert len(multi_calls) >= 2
            first_batch = [
                block
                for block in multi_calls[0]["normalized_blocks"]
                if block.get("kind") == "tool_call"
                and block.get("tool_name") == "view_image"
            ]
            assert len(first_batch) == 3, first_batch
            before_carriers = tool_image_carriers(multi_calls[0])
            carriers = tool_image_carriers(multi_calls[-1])
            assert len(carriers) == len(before_carriers) + 1, carriers
            carrier = carriers[-1]
            image_part_types = {"image_url", "input_image"}
            assert (
                sum(part.get("type") in image_part_types for part in carrier["content"])
                == 2
            )
            multi_image_rows = await asyncio.to_thread(
                tool_image_database_evidence, session
            )
            assert len(multi_image_rows) == 3, multi_image_rows
            workspace_card.unlink()
            workspace_card_two.unlink()
            retained_reply = await turn(
                "tool_images_after_source_delete",
                PromptContent.text(
                    "Without calling any tool, read the two successful images from "
                    "the immediately preceding tool results and reply only with a "
                    "JSON array of their three-digit numbers in tool-call order."
                ),
            )
            retained_value = json.loads(
                retained_reply.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            assert retained_value == [
                fixtures[0]["number"],
                fixtures[1]["number"],
            ], retained_value
            report["checks"]["parallel_mixed_and_deleted_source_replay"] = True

            session_id = session.session_id
            await core.close_session(session.host_session_id, close_conversation=False)
            session = await core.resume_session(
                session_id, workspace_input=workspace_input
            )
            cold_reply = await turn(
                "cold_resume",
                PromptContent.text(
                    "Read the retained view_image result whose source ends in "
                    "view-card-two.png and reply with only its three-digit number."
                ),
                session,
            )
            assert cold_reply.strip() == fixtures[1]["number"], cold_reply
            fork_anchor = await asyncio.to_thread(anchor, session)
            child_id = f"session:{uuid4().hex}"
            creation = await core.fork_conversation(
                source_session_id=session.session_id,
                anchor_entry_id=fork_anchor,
                child_session_id=child_id,
                memory_domain_id="u_local",
            )
            assert creation.created, creation
            await core.close_session(session.host_session_id, close_conversation=True)
            child = await core.resume_session(child_id, workspace_input=workspace_input)
            child_image_rows = await asyncio.to_thread(
                tool_image_database_evidence, child
            )
            assert len(child_image_rows) == 3, child_image_rows
            fork_reply = await turn(
                "fork_after_parent_close",
                PromptContent.text(
                    "Read the retained view_image result whose source ends in "
                    "view-card.png and reply with only its three-digit number."
                ),
                child,
            )
            assert fork_reply.strip() == fixtures[0]["number"], fork_reply
            for phase in ("cold_resume", "fork_after_parent_close"):
                calls = [
                    item
                    for item in report["calls"]
                    if item["phase"] == phase and item["purpose"] == "agent_model_loop"
                ]
                assert calls and calls[-1]["image_occurrences"] > 0
            report["checks"]["cold_resume_and_independent_fork_images"] = True
            report["checks"]["fork_preserves_typed_tool_image"] = True
            session = child

            report["phase"] = "manual_compaction"
            print(f"K4 {args.model}: manual_compaction", flush=True)
            compacted = await session.compact_context(
                command_id=f"command:k4:{uuid4().hex}", force=True
            )
            report["compaction"] = {
                "disposition": compacted.disposition.value,
                "detail": str(compacted),
            }
            assert compacted.disposition.value == "COMPACTED", compacted
            summary = [
                item
                for item in report["calls"]
                if item["purpose"] == "context_compaction_summary"
            ]
            assert summary and any(item["image_occurrences"] > 0 for item in summary)
            reply = await turn(
                "after_compaction",
                PromptContent.text(
                    "Based on the compacted conversation summary, reply only with "
                    "the exact text COMPACT_OK. Do not call any tool."
                ),
                session,
            )
            assert reply.strip() == "COMPACT_OK", reply
            after = [
                item
                for item in report["calls"]
                if item["phase"] == "after_compaction"
                and item["purpose"] == "agent_model_loop"
            ]
            assert after
            report["checks"]["summary_saw_images_and_compacted_successor_completed"] = (
                True
            )

            tool_reply = await turn(
                "tool_followup",
                PromptContent.text(
                    "Call read_file on marker.txt with offset 1 and limit 10, then "
                    "reply with only the file token. Do not use other tools."
                ),
                session,
            )
            assert "K4_TOOL_PINE" in tool_reply, tool_reply
            evidence = _database_evidence(session)
            assert evidence["tool_result_count"] >= 1, evidence
            successor_warm = [
                item
                for item in report["calls"]
                if item["purpose"] == "agent_model_loop"
                and item["phase"] in {"after_compaction", "tool_followup"}
            ]
            check_prefix(successor_warm)
            report["checks"]["warm_prefix_and_tool_followup"] = True

            report["database_evidence"] = _database_evidence(child)
            await core.close_session(child.host_session_id, close_conversation=False)

            for record in report["calls"]:
                assert (
                    record["terminal"] and record["terminal"]["kind"] == "COMPLETED"
                ), record["terminal"]
                matching_payloads = [
                    item
                    for item in report["sdk_payloads"]
                    if item["projection"] == record["projection"]
                ]
                assert matching_payloads
                project = (
                    chat_completions.project_chat_context_bearing_payload_fields
                    if record["wire_api"] == "openai_chat_completions"
                    else responses.project_responses_context_bearing_payload_fields
                )
                requests = [
                    item
                    for item in report["http_requests"]
                    if item["body"].get("model") == record["model_id"]
                    and project(item["body"]) == record["projection"]
                ]
                assert requests and any(item["status_code"] == 200 for item in requests)
            report["checks"]["actual_http_equals_frozen_materialization"] = True
            report["passed"] = True
    finally:
        if core is not None:
            await core.shutdown()
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        await asyncio.to_thread(_drop_database, saved, database)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        required=True,
        help="Exact saved model ID; no credentials on the command line",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--connection-id",
        help="Exact saved connection when the model has more than one API configuration",
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=900,
        help="Finite acceptance run deadline, not a product lifetime cap",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    saved = LocalSettingsStore().read()
    scrub = secret_scrubber(saved)
    report = {
        "schema": "kernel-image-input-k4-dogfood.v1",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "package_path": str(Path(pulsara_agent.__file__).resolve()),
        "saved_home": str(require_pulsara_home()),
        "model": args.model,
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
            with Observers(report):
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
    encoded = json.dumps(scrub.scrub_json(report), ensure_ascii=False, indent=2)
    assert scrub.scrub_text(encoded) == encoded
    (args.output / "report.json").write_text(encoded, encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "phase": report["phase"],
                "calls": len(report["calls"]),
                "checks": report["checks"],
                "failure": scrub.scrub_json(report.get("failure")),
                "report": str(args.output / "report.json"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
