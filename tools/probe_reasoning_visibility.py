"""Opt-in saved Chat connection probe; raw evidence, no settings/DB mutation.

Pulsara owns settings/auth, OpenAI SDK owns transport, and this script owns only
finite experimental cases and evidence. Reuse the existing probe's SSE capture.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import random
from time import monotonic

from tools.probe_vision_tokens import (
    LocalSettingsStore,
    require_pulsara_home,
    secret_scrubber,
    write_json,
    ProcessCredentialBoundary,
    OpenAITransportTimeoutPolicy,
    build_async_openai_client,
    admit_provider_request,
    openai_auth_request_options,
    request_once,
    usage_fields,
)

PROMPTS = {
    "simple": "求 x+y=10 时 xy 的最大值，用两句话解释。",
    "complex": (
        "使用 0 到 9 中不重复的数字组成六位数，首位不能为 0，恰好有三个奇数，"
        "且相邻两位不能都是奇数。这样的六位数中，有多少个能被 11 整除？"
        "请独立计算，给出精确数量及简短、可核验的计数方法。不要调用工具。"
    ),
    "medium": (
        "从 1 到 30 中选三个互不相同的整数，和为 45，其中恰有一个数能被 5 整除。"
        "共有多少组无序三元组？请给出准确数量和简短的可核验计算过程，不要调用工具。"
    ),
}


def options(name, effort):
    if name == "legacy_effort":
        return {"reasoning_effort": effort}
    reasoning = {"effort": effort}
    if name != "nested_effort":
        reasoning["exclude"] = name == "exclude_control"
    if name in {"summary_auto", "summary_detailed"}:
        reasoning["summary"] = name.removeprefix("summary_")
    return {"extra_body": {"reasoning": reasoning}}


def visibility(messages):
    counts = Counter()
    fields = Counter()
    first = None
    for message in messages:
        for key in ("reasoning", "reasoning_content"):
            value = message.get(key)
            if isinstance(value, str):
                fields[key] += len(value)
        for part in message.get("reasoning_details") or []:
            if not isinstance(part, dict):
                continue
            kind = part.get("type", "unknown")
            counts[kind] += 1
            key = {"reasoning.summary": "summary", "reasoning.text": "text"}.get(kind)
            if key and isinstance(part.get(key), str):
                fields[kind] += len(part[key])
                if part[key] and first is None:
                    first = part[key][:160]
    return {
        "public_reasoning": any(fields.values()),
        "public_chars": dict(fields),
        "detail_types": dict(counts),
        "first_public_fragment": first,
    }


async def run(args):
    settings = LocalSettingsStore(require_pulsara_home() / "local-settings.yaml").read()
    connection = next(
        c for c in settings.model_connections if c.id.value == args.connection
    )
    if connection.target.wire_api.value != "openai_chat_completions":
        raise ValueError("This experiment requires a saved Chat Completions connection")
    scrub = secret_scrubber(settings)
    key = settings.require_model_api_key(connection.id)
    boundary = ProcessCredentialBoundary(key)
    client = build_async_openai_client(
        api_key=key,
        base_url=connection.base_url,
        timeout_policy=OpenAITransportTimeoutPolicy(10, 30, 10, 180, 180),
        credential_boundary=boundary,
        max_retries=0,
    )
    root = args.output
    root.mkdir(parents=True, exist_ok=False)
    cases = [
        (name, prompt, "max", True, repeat)
        for name in (
            "legacy_effort",
            "nested_effort",
            "include_reasoning",
            "summary_auto",
            "summary_detailed",
        )
        for prompt in ("simple", "complex")
        for repeat in range(3)
    ]
    cases += [
        (name, prompt, "max", False, repeat)
        for name in ("legacy_effort", "summary_auto")
        for prompt in ("simple", "complex")
        for repeat in range(2)
    ]
    cases += [
        (name, "simple", "high", True, repeat)
        for name in ("legacy_effort", "summary_auto")
        for repeat in range(3)
    ]
    cases += [
        ("exclude_control", prompt, "max", True, 0) for prompt in ("simple", "complex")
    ]
    if args.suite == "medium":
        cases = [
            (name, "medium", effort, True, repeat)
            for name in ("legacy_effort", "summary_auto")
            for effort in ("high", "max")
            for repeat in range(2)
        ]
    random.Random(1709).shuffle(cases)
    write_json(
        root / "manifest.json",
        {
            "connection_id": connection.id.value,
            "model": connection.target.model_id,
            "base_url": connection.base_url,
            "cases": cases,
            "prompts": PROMPTS,
            "concurrency": 3,
            "output_token_limit": 8192,
            "experiment_timeout_seconds": 180,
        },
        scrub,
    )
    rows = []
    semaphore = asyncio.Semaphore(3)

    async def one(case):
        name, prompt, effort, streaming, repeat = case
        identity = f"{name}-{prompt}-{effort}-{'stream' if streaming else 'nonstream'}-{repeat}"
        directory = root / identity
        payload = {
            "model": connection.target.model_id,
            "messages": [{"role": "user", "content": PROMPTS[prompt]}],
            "max_completion_tokens": 8192,
            "stream": streaming,
            **options(name, effort),
        }
        if streaming:
            payload["stream_options"] = {"include_usage": True}
        async with semaphore:
            if streaming:
                row = await request_once(
                    connection, client, boundary, payload, directory, 180, scrub
                )
                messages = []
                generation_id = None
                if (directory / "events.jsonl").exists():
                    for line in (directory / "events.jsonl").read_text().splitlines():
                        event = json.loads(line)
                        generation_id = event.get("id", generation_id)
                        for choice in event.get("choices", []):
                            messages.append(choice.get("delta") or {})
                row["generation_id"] = generation_id
            else:
                directory.mkdir()
                write_json(directory / "request.json", payload, scrub)
                started = monotonic()
                row = {}
                messages = []
                try:
                    async with asyncio.timeout(180):
                        response = await admit_provider_request(
                            credential_boundary=boundary,
                            payload=payload,
                            operation=lambda: client.chat.completions.create(
                                **payload,
                                **openai_auth_request_options(
                                    requires_api_key=connection.requires_api_key
                                ),
                            ),
                        )
                    raw = response.model_dump(mode="json", exclude_none=True)
                    write_json(directory / "response.json", raw, scrub)
                    messages = [
                        choice.get("message") or {} for choice in raw.get("choices", [])
                    ]
                    row = {
                        "status": "ok",
                        "reported_model": raw.get("model"),
                        "reported_provider": raw.get("provider"),
                        "usage": raw.get("usage"),
                        "generation_id": raw.get("id"),
                        "finish_reason": raw["choices"][0].get("finish_reason"),
                        "answer": raw["choices"][0].get("message", {}).get("content"),
                        **usage_fields(raw.get("usage")),
                    }
                except Exception as exc:
                    row = {
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "error": scrub.scrub_text(str(exc)),
                    }
                row["elapsed_seconds"] = round(monotonic() - started, 3)
            row.update(
                case=name,
                prompt=prompt,
                effort=effort,
                streaming=streaming,
                repeat=repeat,
                evidence=str(directory),
                **visibility(messages),
            )
            write_json(directory / "result.json", row, scrub)
            rows.append(row)
            write_json(root / "results.json", rows, scrub)
            print(
                json.dumps(
                    {
                        "done": len(rows),
                        "total": len(cases),
                        "case": identity,
                        "status": row["status"],
                        "public": row["public_reasoning"],
                        "reasoning_tokens": row.get("reasoning_tokens"),
                        "finish": row.get("finish_reason"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    try:
        await asyncio.gather(*(one(case) for case in cases))
    finally:
        await client.close()
    groups = defaultdict(list)
    for row in rows:
        groups[(row["case"], row["prompt"], row["effort"], row["streaming"])].append(
            row
        )
    summary = []
    for key, values in sorted(groups.items()):
        complete = [
            r
            for r in values
            if r["status"] == "ok" and r.get("finish_reason") == "stop"
        ]
        summary.append(
            {
                "case": key,
                "total": len(values),
                "completed": len(complete),
                "public": sum(r["public_reasoning"] for r in complete),
                "reasoning_tokens": [r.get("reasoning_tokens") for r in complete],
            }
        )
    write_json(root / "summary.json", summary, scrub)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection", required=True)
    parser.add_argument("--suite", choices=("main", "medium"), default="main")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output") / f"reasoning-visibility-{datetime.now():%Y%m%d-%H%M%S}",
    )
    asyncio.run(run(parser.parse_args()))
