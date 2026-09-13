"""Offline synthetic images and an opt-in, saved-connection vision usage probe.

This is a D1 experiment, not kernel image activation or a production estimator.
Pillow owns image codecs; Pulsara owns settings/auth/client construction; the
OpenAI SDK owns HTTP/SSE. Only the experimental multipart request is built here.
No database, saved settings, provider model tables, or runtime budgets are changed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import random
from time import monotonic
from typing import Any

import openai
from PIL import Image, ImageDraw, ImageFont, __version__ as pillow_version

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.llm.adapters.openai.client import (
    OpenAITransportTimeoutPolicy,
    admit_provider_request,
    build_async_openai_client,
    openai_auth_request_options,
)
from pulsara_agent.llm.model_catalog import WireApi
from pulsara_agent.llm.model_connections import ModelConnectionConfig
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialScrubSet,
)
from pulsara_agent.settings import LocalSettings, LocalSettingsStore


PROMPT = (
    "Read the large word in the top-left panel of the attached image(s). "
    "Return that word only. If there is no image or no readable word, return NONE."
)
# A user-requested exclusion for an unavailable LAN deployment, not a capability rule.
DEFAULT_EXCLUDED_MODELS = ("Qwen3.8-27B",)


@dataclass(frozen=True)
class Case:
    name: str
    width: int = 0
    height: int = 0
    style: str = "scene"
    codec: str = "PNG"
    compression: int = 9
    quality: int = 85
    copies: int = 1
    turns: int = 1


def experiment_cases(suite: str) -> list[Case]:
    """Finite experimental samples; these are not production admission limits."""
    if suite == "d1":
        return [
            Case("text_baseline", copies=0),
            Case("text_baseline_turns3", copies=0, turns=3),
            *(
                Case(f"scene_{n}", n, n)
                for n in (64, 128, 256, 399, 400, 401, 1008, 1009)
            ),
            *(
                Case(f"scene_{w}x{h}", w, h)
                for w, h in ((1365, 768), (1920, 1080), (1080, 1920), (2560, 1440))
            ),
            *(Case(f"scene_1024_copies{n}", 1024, 1024, copies=n) for n in (3, 4, 8)),
            Case("scene_128_copies8", 128, 128, copies=8),
            Case("scene_1024_turns3", 1024, 1024, copies=3, turns=3),
        ]
    cases = [
        Case("text_baseline", copies=0),
        Case("scene_512", 512, 512),
        Case("scene_1024", 1024, 1024),
        Case("scene_2048", 2048, 2048),
        Case("scene_1024_png_raw", 1024, 1024, compression=0),
        Case("scene_1024_jpeg85", 1024, 1024, codec="JPEG"),
        Case("noise_1024", 1024, 1024, style="noise"),
        Case("scene_1024_twice", 1024, 1024, copies=2),
    ]
    if suite == "full":
        cases.extend(
            Case(f"scene_{n}", n, n)
            for n in (
                31,
                32,
                33,
                383,
                384,
                385,
                511,
                513,
                767,
                768,
                769,
                1536,
                4096,
            )
        )
        cases.extend(
            [
                Case("scene_2048x512", 2048, 512),
                Case("scene_512x2048", 512, 2048),
                Case("scene_4096x256", 4096, 256),
                Case("scene_256x4096", 256, 4096),
                Case("scene_1024_jpeg30", 1024, 1024, codec="JPEG", quality=30),
                Case("scene_1024_webp80", 1024, 1024, codec="WEBP", quality=80),
            ]
        )
    return cases


def render_image(case: Case) -> Image.Image:
    if case.style == "noise":
        return Image.frombytes(
            "RGB",
            (case.width, case.height),
            random.Random(1701).randbytes(case.width * case.height * 3),
        )
    # One source scene, deterministically scaled; codec variants share exact pixels.
    image = Image.new("RGB", (1024, 1024), "#f4f6fa")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=58)
    small = ImageFont.load_default(size=24)
    draw.rounded_rectangle((32, 32, 640, 156), radius=16, fill="#ffffff")
    draw.text((64, 60), "ORBIT", fill="#10243a", font=font)
    draw.text((40, 192), "Synthetic dashboard / 0123456789", fill="#203a54", font=small)
    for i, color in enumerate(("#2864dc", "#09a879", "#f2a93b", "#ce4773")):
        x = 48 + i * 240
        draw.rectangle((x, 260, x + 180, 430 + i * 60), fill=color)
        draw.text((x, 660), str((i + 1) * 17), font=small, fill="#203a54")
    for i in range(8):
        y = 750 + i * 28
        draw.line((40, y, 970, y), fill="#b7c5d8", width=2)
    draw.ellipse((770, 35, 955, 220), fill="#f2a93b", outline="#10243a", width=5)
    return image.resize((case.width, case.height), Image.Resampling.LANCZOS)


def make_fixtures(cases: list[Case], directory: Path) -> dict[str, dict[str, Any]]:
    directory.mkdir(parents=True)
    fixtures: dict[str, dict[str, Any]] = {}
    for case in cases:
        if not case.copies:
            continue
        suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[case.codec]
        path = directory / (case.name + suffix)
        with render_image(case) as image:
            options = (
                {"compress_level": case.compression}
                if case.codec == "PNG"
                else {"quality": case.quality}
            )
            image.save(path, format=case.codec, **options)
        with Image.open(path) as image:
            image.load()
            assert image.size == (case.width, case.height)
        fixtures[case.name] = {
            **asdict(case),
            "path": str(path),
            "file_bytes": path.stat().st_size,
            "mime": Image.MIME[case.codec],
            "expected_word": "NONE" if case.style == "noise" else "ORBIT",
        }
    return fixtures


def make_payload(
    connection: ModelConnectionConfig,
    fixture: dict[str, Any] | None,
    detail: str,
    output_tokens: int,
    turns: int = 1,
) -> dict[str, Any]:
    responses = connection.target.wire_api is WireApi.OPENAI_RESPONSES
    parts: list[dict[str, Any]] = [
        {"type": "input_text" if responses else "text", "text": PROMPT}
    ]
    if fixture:
        encoded = base64.b64encode(Path(fixture["path"]).read_bytes()).decode("ascii")
        url = f"data:{fixture['mime']};base64,{encoded}"
        for _ in range(fixture["copies"]):
            parts.append(
                {"type": "input_image", "image_url": url, "detail": detail}
                if responses
                else {"type": "image_url", "image_url": {"url": url, "detail": detail}}
            )
    payload: dict[str, Any] = {"model": connection.target.model_id, "stream": True}
    messages = []
    for turn in range(turns):
        if turn:
            messages.append({"role": "assistant", "content": "Acknowledged."})
        # Each layout has an identical text-only baseline. Images are distributed
        # in order across turns; the chosen multi-turn case has one per turn.
        start = (len(parts) - 1) * turn // turns
        end = (len(parts) - 1) * (turn + 1) // turns
        messages.append(
            {"role": "user", "content": parts[:1] + parts[1 + start : 1 + end]}
        )
    payload["input" if responses else "messages"] = messages
    if responses:
        payload.update(store=False, max_output_tokens=output_tokens)
    else:
        payload.update(
            max_completion_tokens=output_tokens, stream_options={"include_usage": True}
        )
    return payload


def secret_scrubber(settings: LocalSettings) -> ProcessCredentialScrubSet:
    scrub = ProcessCredentialScrubSet()
    for item in settings.model_api_keys + settings.mcp_credentials:
        scrub.observe(item.value)
    scrub.observe(settings.dashscope_credentials.embedding_api_key)
    scrub.observe(settings.dashscope_credentials.rerank_api_key)

    def oauth_values(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {
                    "access_token",
                    "refresh_token",
                    "client_secret",
                    "id_token",
                } and isinstance(child, str):
                    scrub.observe(child)
                else:
                    oauth_values(child)
        elif isinstance(value, list):
            for child in value:
                oauth_values(child)

    for item in settings.mcp_oauth:
        for raw in (item.token_json, item.client_json, item.auth_json):
            oauth_values(json.loads(raw))
    return scrub


def write_json(path: Path, value: object, scrub: ProcessCredentialScrubSet) -> None:
    path.write_text(
        scrub.scrub_text(json.dumps(value, ensure_ascii=False, indent=2)) + "\n"
    )


def usage_fields(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Missing usage is unknown, never zero; cached tokens stay in input totals."""
    usage = usage or {}
    details = (
        usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    )
    output_details = (
        usage.get("output_tokens_details")
        or usage.get("completion_tokens_details")
        or {}
    )
    return {
        "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
        "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
        "cached_tokens": details.get("cached_tokens"),
        "reasoning_tokens": output_details.get("reasoning_tokens"),
    }


async def request_once(
    connection: ModelConnectionConfig,
    client: Any,
    boundary: ProcessCredentialBoundary,
    payload: dict[str, Any],
    directory: Path,
    timeout: float,
    scrub: ProcessCredentialScrubSet,
) -> dict[str, Any]:
    directory.mkdir(parents=True)
    write_json(directory / "request.json", payload, scrub)
    row: dict[str, Any] = {
        "status": "started",
        "reported_model": None,
        "reported_provider": None,
        "usage": None,
        "answer": "",
        "finish_reason": None,
        "request_json_utf8_bytes": len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        ),
        "evidence": str(directory),
    }
    started = monotonic()
    stream = None
    terminal = False
    try:
        async with asyncio.timeout(timeout):
            resource = (
                client.responses
                if connection.target.wire_api is WireApi.OPENAI_RESPONSES
                else client.chat.completions
            )
            stream = await admit_provider_request(
                credential_boundary=boundary,
                payload=payload,
                operation=lambda: resource.create(
                    **payload,
                    **openai_auth_request_options(
                        requires_api_key=connection.requires_api_key
                    ),
                ),
            )
            with (directory / "events.jsonl").open("w") as events:
                async for event in stream:
                    raw = event.model_dump(mode="json", exclude_none=True)
                    events.write(
                        scrub.scrub_text(json.dumps(raw, ensure_ascii=False)) + "\n"
                    )
                    events.flush()
                    if raw.get("error"):
                        raise RuntimeError(json.dumps(raw["error"]))
                    if "choices" in raw:
                        row["reported_model"] = raw.get("model", row["reported_model"])
                        row["reported_provider"] = raw.get(
                            "provider", row["reported_provider"]
                        )
                        if raw.get("usage") is not None:
                            row["usage"] = raw["usage"]
                        for choice in raw["choices"]:
                            row["answer"] += (
                                choice.get("delta", {}).get("content") or ""
                            )
                            if choice.get("finish_reason"):
                                terminal = True
                                row["finish_reason"] = choice["finish_reason"]
                    elif raw.get("type") == "response.output_text.delta":
                        row["answer"] += raw.get("delta", "")
                    elif raw.get("type") in {
                        "response.completed",
                        "response.incomplete",
                        "response.failed",
                    }:
                        response = raw["response"]
                        row["reported_model"] = response.get("model")
                        row["usage"] = response.get("usage")
                        row["finish_reason"] = response.get("status")
                        row["incomplete_details"] = response.get("incomplete_details")
                        terminal = True
                        if response.get("error") or raw["type"] == "response.failed":
                            raise RuntimeError(
                                json.dumps(response.get("error") or response)
                            )
            if not terminal:
                raise RuntimeError(
                    "Stream ended without an observable protocol terminal"
                )
            row["status"] = (
                "ok"
                if usage_fields(row["usage"])["input_tokens"] is not None
                else "usage_missing"
            )
    except Exception as exc:
        row["status"] = "error"
        row["error_type"] = type(exc).__name__
        row["error"] = str(exc) or (
            f"Request exceeded the {timeout}s experiment deadline"
            if isinstance(exc, TimeoutError)
            else type(exc).__name__
        )
        if isinstance(exc, openai.APIStatusError):
            row["http_status"] = exc.status_code
            row["error_body"] = exc.response.text
    finally:
        if stream is not None:
            await stream.close()
    row["elapsed_seconds"] = round(monotonic() - started, 3)
    row.update(usage_fields(row["usage"]))
    write_json(directory / "result.json", row, scrub)
    return row


def compare_to_baseline(row: dict[str, Any], baseline: dict[str, Any] | None) -> None:
    comparable = bool(
        baseline
        and baseline["status"] == "ok"
        and row["status"] == "ok"
        and baseline.get("reported_model") == row.get("reported_model")
        and baseline.get("reported_provider") == row.get("reported_provider")
        and isinstance(baseline.get("input_tokens"), int)
        and isinstance(row.get("input_tokens"), int)
    )
    difference = row["input_tokens"] - baseline["input_tokens"] if comparable else None
    # Keep the observation, but never treat negative overhead as visual cost.
    row["raw_input_difference"] = difference
    row["comparison_issue"] = (
        "negative_input_difference"
        if difference is not None and difference < 0
        else None
        if comparable
        else "baseline_or_reported_identity_unavailable"
    )
    row["baseline_comparable"] = comparable and row["comparison_issue"] is None
    row["input_delta_vs_text_baseline"] = (
        difference if row["baseline_comparable"] else None
    )


async def run_probe(
    args: argparse.Namespace,
    settings: LocalSettings,
    connections: list[ModelConnectionConfig],
    cases: list[Case],
    fixtures: dict[str, dict[str, Any]],
    output: Path,
    scrub: ProcessCredentialScrubSet,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # Concurrency is a physical experiment scheduling bound, not a logical work cap.
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one_connection(connection: ModelConnectionConfig, ordinal: int) -> None:
        async with semaphore:
            key = settings.model_api_key(connection.id)
            boundary = ProcessCredentialBoundary(key or "")
            policy = OpenAITransportTimeoutPolicy(
                10, 30, 10, args.timeout, args.timeout
            )
            client = build_async_openai_client(
                api_key=key,
                base_url=connection.base_url,
                timeout_policy=policy,
                credential_boundary=boundary,
                max_retries=0,
            )
            try:
                for repeat in range(1, args.repeat + 1):
                    for detail in args.detail:
                        baselines: dict[int, dict[str, Any]] = {}
                        for case in cases:
                            baseline = baselines.get(case.turns)
                            fixture = fixtures.get(case.name)
                            identity = {
                                "connection_id": connection.id.value,
                                "model": connection.target.model_id,
                                "route": connection.target.route_id,
                                "wire_api": connection.target.wire_api.value,
                                "case": case.name,
                                "detail": detail,
                                "repeat": repeat,
                                "width": case.width,
                                "height": case.height,
                                "copies": case.copies,
                                "turns": case.turns,
                                "file_bytes": fixture["file_bytes"] if fixture else 0,
                            }
                            if baseline is not None and baseline["status"] != "ok":
                                row = {
                                    **identity,
                                    "status": "skipped",
                                    "error": "text baseline failed or returned no input usage",
                                }
                            else:
                                payload = make_payload(
                                    connection,
                                    fixture,
                                    detail,
                                    args.output_tokens,
                                    case.turns,
                                )
                                directory = (
                                    output
                                    / "requests"
                                    / f"{ordinal:02d}"
                                    / f"r{repeat}_{detail}_{case.name}"
                                )
                                row = {
                                    **identity,
                                    **await request_once(
                                        connection,
                                        client,
                                        boundary,
                                        payload,
                                        directory,
                                        args.timeout,
                                        scrub,
                                    ),
                                }
                                expected = (
                                    fixture["expected_word"] if fixture else "NONE"
                                )
                                row["expected_word"] = expected
                                row["answer_match"] = (
                                    row["answer"].strip().strip(".\"'` ").upper()
                                    == expected
                                )
                                if not case.copies:
                                    baseline = row
                                    baselines[case.turns] = row
                                compare_to_baseline(row, baseline)
                            rows.append(row)
                            with (output / "results.jsonl").open("a") as sink:
                                sink.write(
                                    scrub.scrub_text(
                                        json.dumps(row, ensure_ascii=False)
                                    )
                                    + "\n"
                                )
                            print(
                                scrub.scrub_text(
                                    f"{connection.target.model_id} | {case.name} | {row['status']} | "
                                    f"input={row.get('input_tokens')} delta={row.get('input_delta_vs_text_baseline')} "
                                    f"answer={row.get('answer', '')!r}"
                                ),
                                flush=True,
                            )
            finally:
                await client.close()

    await asyncio.gather(*(one_connection(c, i) for i, c in enumerate(connections, 1)))
    return rows


def write_summary(
    output: Path, rows: list[dict[str, Any]], scrub: ProcessCredentialScrubSet
) -> None:
    baselines = {
        (r["connection_id"], r["repeat"], r["detail"], r.get("turns", 1)): r
        for r in rows
        if not r["copies"]
    }
    for row in rows:
        compare_to_baseline(
            row,
            baselines.get(
                (
                    row["connection_id"],
                    row["repeat"],
                    row["detail"],
                    row.get("turns", 1),
                )
            ),
        )
    # Final derived fields are refreshed without changing requests, events or usage.
    (output / "results.jsonl").write_text(
        "".join(
            scrub.scrub_text(json.dumps(row, ensure_ascii=False)) + "\n" for row in rows
        )
    )
    fields = [
        "model",
        "connection_id",
        "route",
        "wire_api",
        "case",
        "detail",
        "repeat",
        "status",
        "width",
        "height",
        "copies",
        "turns",
        "file_bytes",
        "input_tokens",
        "input_delta_vs_text_baseline",
        "raw_input_difference",
        "comparison_issue",
        "cached_tokens",
        "output_tokens",
        "reasoning_tokens",
        "reported_model",
        "reported_provider",
        "elapsed_seconds",
        "answer_match",
        "finish_reason",
        "error",
    ]
    with (output / "results.csv").open("w", newline="") as sink:
        writer = csv.DictWriter(sink, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(scrub.scrub_json(rows))
    lines = [
        "# Vision token probe",
        "",
        "Input deltas include image-related framing. They are provider-reported usage differences, not proven internal visual sequence lengths.",
        "Cached input tokens are not subtracted. A valid usage record does not prove OCR succeeded; see answer_match and the raw events.",
        "Negative differences remain in raw_input_difference and are excluded from the usable delta column. Hidden upstream prompt/routing changes may also invalidate positive differences.",
        "No provider-specific constants are promoted into the production estimator.",
        "",
        "| Model | Case | Status | Input | Delta vs text | Answer | Seconds |",
        "|---|---|---|---:|---:|---|---:|",
    ]
    for row in rows:
        values = [
            row.get(k)
            for k in (
                "model",
                "case",
                "status",
                "input_tokens",
                "input_delta_vs_text_baseline",
                "answer",
                "elapsed_seconds",
            )
        ]
        lines.append(
            "| "
            + " | ".join(
                str(v if v is not None else "—").replace("|", "\\|").replace("\n", " ")
                for v in values
            )
            + " |"
        )
    (output / "report.md").write_text(scrub.scrub_text("\n".join(lines)) + "\n")


def positive_int(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Send the selected finite matrix to saved connections; otherwise generate fixtures only.",
    )
    parser.add_argument("--suite", choices=("smoke", "full", "d1"), default="smoke")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Case name; repeatable. Text baseline is always included.",
    )
    parser.add_argument(
        "--connection",
        action="append",
        default=[],
        help="Saved connection ID or model ID; repeatable.",
    )
    parser.add_argument("--exclude-model", action="append", default=[])
    parser.add_argument(
        "--include-lan",
        action="store_true",
        help="Explicitly opt the currently unavailable Qwen3.8-27B LAN deployment back in.",
    )
    parser.add_argument("--detail", action="append", choices=("auto", "low", "high"))
    parser.add_argument("--repeat", type=positive_int, default=1)
    parser.add_argument("--concurrency", type=positive_int, default=2)
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=90,
        help="Per-request experimental timeout, in seconds; no automatic retries.",
    )
    parser.add_argument(
        "--output-tokens",
        type=positive_int,
        default=256,
        help="Per-request output budget for the short OCR check, including reasoning where applicable.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.detail = args.detail or ["auto"]
    cases = experiment_cases(args.suite)
    unknown = set(args.case) - {c.name for c in cases}
    if unknown:
        parser.error(f"unknown cases for this suite: {sorted(unknown)}")
    if args.case:
        cases = [c for c in cases if not c.copies or c.name in args.case]
    settings = LocalSettingsStore().read()
    scrub = secret_scrubber(settings)
    excluded = set(args.exclude_model) | (
        set() if args.include_lan else set(DEFAULT_EXCLUDED_MODELS)
    )
    selectors = {
        v for c in settings.model_connections for v in (c.id.value, c.target.model_id)
    }
    if set(args.connection) - selectors:
        parser.error("unknown saved connection selector")
    connections = [
        c
        for c in settings.model_connections
        if c.target.model_id not in excluded
        and (
            not args.connection
            or c.id.value in args.connection
            or c.target.model_id in args.connection
        )
    ]
    if not connections:
        parser.error("no selected saved model connections")
    output = (
        args.output
        or Path("scratch/vision-token-probe")
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    ).resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixtures = make_fixtures(cases, output / "images")
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "effective_pulsara_home": str(require_pulsara_home()),
        "pillow_version": pillow_version,
        "openai_version": openai.__version__,
        "prompt": PROMPT,
        "suite": args.suite,
        "detail": args.detail,
        "repeats": args.repeat,
        "output_tokens": args.output_tokens,
        "timeout_seconds": args.timeout,
        "concurrency": args.concurrency,
        "reasoning_selection": "provider default; no experimental provider-specific override",
        "excluded_models": sorted(excluded),
        "cases": [asdict(c) for c in cases],
        "fixtures": fixtures,
        "connections": [
            {
                "id": c.id.value,
                "model": c.target.model_id,
                "route": c.target.route_id,
                "wire_api": c.target.wire_api.value,
                "base_url": c.base_url,
            }
            for c in connections
        ],
        "planned_requests": len(connections)
        * len(cases)
        * len(args.detail)
        * args.repeat,
    }
    write_json(output / "manifest.json", manifest, scrub)
    print(
        f"Output: {output}\nSelected connections: {len(connections)}; planned requests: {manifest['planned_requests']}",
        flush=True,
    )
    if args.run:
        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        rows = asyncio.run(
            run_probe(args, settings, connections, cases, fixtures, output, scrub)
        )
        write_summary(output, rows, scrub)
        print(f"Report: {output / 'report.md'}", flush=True)
    else:
        print("Generated fixtures only. Add --run to execute this matrix.", flush=True)


if __name__ == "__main__":
    main()
