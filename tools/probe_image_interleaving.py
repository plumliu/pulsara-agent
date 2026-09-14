"""Saved-provider experiment for ordered text/image USER content; not kernel activation.

Reuse the existing probe's SDK transport, credentials, evidence, and scrubbing.
Pillow generates public synthetic cards. Only experimental content assembly and
answer grading live here; no settings/database/production contracts are changed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import defaultdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import random

from PIL import Image, ImageDraw, ImageFont

from pulsara_agent.llm.model_catalog import WireApi
from tools.probe_vision_tokens import (
    LocalSettingsStore,
    OpenAITransportTimeoutPolicy,
    ProcessCredentialBoundary,
    build_async_openai_client,
    request_once,
    require_pulsara_home,
    secret_scrubber,
    write_json,
)

CASES = (
    "text_baseline",
    "text_first",
    "images_first",
    "interleave_before",
    "interleave_after",
    "interleave_swap",
    "interleave_four_repeat",
)
LABELS = ("delta", "alpha", "zulu", "bravo")
INSTRUCTION = (
    "Read the three-digit number printed in each attached image. "
    "Associate each image with its assigned text label. "
    "Reply with only one JSON object mapping labels to three-digit strings. "
    "If an image is absent or unreadable, use null for its label. "
    "Do not guess. Labels are assigned by text, not by anything printed in images. "
)
STABLE_MODELS = {
    "qwen3.8-flash",
    "meta/muse-spark-1.3-contributor",
    "openai/gpt-5.6-luna",
    "deepseek-flash",
    "glm-5.3-flash",
}


def fixtures(directory, repeats, seed):
    directory.mkdir(parents=True)
    numbers = random.Random(seed).sample(range(100, 1000), repeats * 3)
    result = []
    for r in range(repeats):
        cards = []
        for i in range(3):
            number = str(numbers[r * 3 + i])
            path = directory / f"r{r + 1}_card{i + 1}.png"
            with Image.new("RGB", (512, 384), "white") as im:
                draw = ImageDraw.Draw(im)
                draw.rectangle((12, 12, 499, 371), outline="#222222", width=4)
                draw.text(
                    (256, 192),
                    number,
                    anchor="mm",
                    fill="black",
                    font=ImageFont.load_default(size=148),
                )
                im.save(path, format="PNG", compress_level=9)
            with Image.open(path) as im:
                im.load()
                assert im.size == (512, 384)
            cards.append({"path": str(path), "number": number})
        result.append(cards)
    return result


def make_case(name, cards):
    # Abstract parts are experiment-only input, never persisted kernel authority.
    def text(value):
        return {"type": "text", "text": value}

    def image(index):
        return {"type": "image", "path": cards[index]["path"]}

    labels = LABELS if name == "interleave_four_repeat" else LABELS[:2]
    order = [0, 1, 0, 2] if len(labels) == 4 else [0, 1]
    if name == "interleave_swap":
        order = [1, 0]
    expected = {label: cards[i]["number"] for label, i in zip(labels, order)}
    assignment = " In image order, the labels are: " + ", ".join(labels) + "."
    if name == "text_baseline":
        parts = [text(INSTRUCTION + assignment)]
        expected = dict.fromkeys(labels)
    elif name == "text_first":
        parts = [text(INSTRUCTION + assignment)] + [image(i) for i in order]
    elif name == "images_first":
        parts = [image(i) for i in order] + [text(INSTRUCTION + assignment)]
    elif name == "interleave_after":
        parts = []
        for position, (label, i) in enumerate(zip(labels, order)):
            parts.extend(
                [
                    image(i),
                    text(
                        (INSTRUCTION if position == 0 else "")
                        + f"The image immediately before this text is labeled {label}."
                    ),
                ]
            )
    else:
        parts = []
        for position, (label, i) in enumerate(zip(labels, order)):
            parts.extend(
                [
                    text(
                        (INSTRUCTION if position == 0 else "")
                        + f"The image immediately after this text is labeled {label}."
                    ),
                    image(i),
                ]
            )
        parts.append(text("Return the JSON object for all assigned labels now."))
    text_only = " ".join(p["text"] for p in parts if p["type"] == "text")
    assert all(card["number"] not in text_only for card in cards), (
        "Answer leaked into prompt"
    )
    return {
        "name": name,
        "parts": parts,
        "expected": expected,
        "pattern": "".join("T" if p["type"] == "text" else "I" for p in parts),
    }


def make_payload(connection, case, output_tokens):
    responses = connection.target.wire_api is WireApi.OPENAI_RESPONSES
    parts = []
    for part in case["parts"]:
        if part["type"] == "text":
            parts.append(
                {"type": "input_text" if responses else "text", "text": part["text"]}
            )
        else:
            encoded = base64.b64encode(Path(part["path"]).read_bytes()).decode("ascii")
            url = "data:image/png;base64," + encoded
            parts.append(
                {"type": "input_image", "image_url": url, "detail": "auto"}
                if responses
                else {"type": "image_url", "image_url": {"url": url, "detail": "auto"}}
            )
    payload = {
        "model": connection.target.model_id,
        "stream": True,
        "input" if responses else "messages": [{"role": "user", "content": parts}],
    }
    if responses:
        payload.update(store=False, max_output_tokens=output_tokens)
    else:
        payload.update(
            max_completion_tokens=output_tokens, stream_options={"include_usage": True}
        )
    return payload


def grade(answer, expected):
    cleaned = answer.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        actual = json.loads(cleaned)
    except (ValueError, IndexError):
        return {"answer_json": None, "strict_match": False, "association_match": False}
    # Numeric JSON instead of strings is a format deviation, not an OCR/order error.
    normalized = (
        {k: str(v) if type(v) is int else v for k, v in actual.items()}
        if isinstance(actual, dict)
        else actual
    )
    return {
        "answer_json": actual,
        "strict_match": actual == expected,
        "association_match": normalized == expected,
    }


def summary(output, rows, scrub):
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["model"], row["wire_api"])].append(row)
    lines = [
        "# 图文交错 provider 实验",
        "",
        "独立 SDK 请求；未经过尚未实现的 Pulsara kernel 图片链路。所有测试卡由 Python/Pillow 生成，数字未写入提示词。",
        "",
        "| 配置模型 | Wire API | 请求 | 协议完成 | 图文关联正确 | JSON 值严格匹配 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for (model, wire), group in grouped.items():
        lines.append(
            f"| {model} | {wire} | {len(group)} | "
            f"{sum(x['protocol_complete'] for x in group)} | "
            f"{sum(x['association_match'] for x in group)} | "
            f"{sum(x['strict_match'] for x in group)} |"
        )
    lines += [
        "",
        "T = 一个 text part，I = 一个 image part。每次仅一条 USER 消息；默认 detail=auto、reasoning 为 provider default。",
        "",
        "| 模型 | 轮次 | 场景 | 顺序 | 协议完成 | 关联正确 | 回复或错误 |",
        "| --- | ---: | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        answer = (
            (row.get("answer") or row.get("error", ""))
            .replace("\n", " ")
            .replace("|", "\\|")
        )
        lines.append(
            f"| {row['model']} | {row['repeat']} | {row['case']} | {row['pattern']} | "
            f"{row['protocol_complete']} | {row['association_match']} | {answer} |"
        )
    lines += [
        "",
        "完整 SDK 调用参数、流式事件及结果保存在 requests/；素材与期望值在 manifest.json。",
        "请求 JSON 的字节数是 SDK 参数序列化测量，不是 HTTP 抓包。成功表示所测 endpoint 接纳并返回可核对答案，不证明服务端内部完全不重排。",
        "样本是清晰数字卡，不能推导所有图像、长上下文或 provider 的质量。无图基线只检验不凭空编造；usage 不作为关联评分。",
        "内网 Qwen3.8-27B 明确排除。gpt-5.5 Responses 为此前不稳定配置，单独看待；首轮若连基线都无法协议完成，仅再执行 grouped/interleaved 两个诊断请求。",
        "[OpenAI 官方图片输入说明](https://developers.openai.com/api/docs/guides/images-vision)允许同一 content 数组中的文本与多张图片；兼容服务行为以本次记录为准。",
        "",
    ]
    (output / "report.zh.md").write_text(scrub.scrub_text("\n".join(lines)))


async def run(args, settings, connections, cases, output, scrub):
    rows = []
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one_connection(connection, ordinal):
        key = settings.model_api_key(connection.id)
        boundary = ProcessCredentialBoundary(key or "")
        client = build_async_openai_client(
            api_key=key,
            base_url=connection.base_url,
            timeout_policy=OpenAITransportTimeoutPolicy(
                10, 30, 10, args.timeout, args.timeout
            ),
            credential_boundary=boundary,
            max_retries=0,
        )
        diagnostic_only = False
        try:
            for repeat, batch in enumerate(cases, 1):
                for case in batch:
                    if diagnostic_only and (
                        repeat > 1
                        or case["name"] not in {"text_first", "interleave_before"}
                    ):
                        continue
                    async with semaphore:
                        result = await request_once(
                            connection,
                            client,
                            boundary,
                            make_payload(connection, case, args.output_tokens),
                            output
                            / "requests"
                            / f"{ordinal:02d}"
                            / f"r{repeat}_{case['name']}",
                            args.timeout,
                            scrub,
                        )
                    complete = result["status"] in {"ok", "usage_missing"}
                    row = {
                        "connection_id": connection.id.value,
                        "model": connection.target.model_id,
                        "wire_api": connection.target.wire_api.value,
                        "repeat": repeat,
                        "case": case["name"],
                        "pattern": case["pattern"],
                        "expected": case["expected"],
                        **result,
                        "protocol_complete": complete,
                        **grade(result.get("answer", ""), case["expected"]),
                    }
                    rows.append(row)
                    with (output / "results.jsonl").open("a") as sink:
                        sink.write(
                            scrub.scrub_text(json.dumps(row, ensure_ascii=False)) + "\n"
                        )
                    summary(output, rows, scrub)
                    print(
                        scrub.scrub_text(
                            f"{row['model']} r{repeat} {case['name']} {case['pattern']} | "
                            f"{result['status']} association={row['association_match']} | "
                            f"{result.get('answer') or result.get('error', '')}"
                        ),
                        flush=True,
                    )
                    if (
                        connection.target.wire_api is WireApi.OPENAI_RESPONSES
                        and repeat == 1
                        and case["name"] == "text_baseline"
                        and not complete
                    ):
                        diagnostic_only = True
        finally:
            await client.close()

    await asyncio.gather(*(one_connection(c, i) for i, c in enumerate(connections, 1)))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--connection", action="append", default=[])
    parser.add_argument("--include-responses", action="store_true")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260913)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--output-tokens", type=int, default=2048)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.repeat, args.timeout, args.output_tokens, args.concurrency) <= 0:
        parser.error("experiment bounds must be positive")
    if args.repeat * 3 > 900:
        parser.error(
            "not enough unique three-digit synthetic numbers; choose a smaller experiment"
        )
    settings = LocalSettingsStore().read()
    scrub = secret_scrubber(settings)
    selected_models = STABLE_MODELS | ({"gpt-5.5"} if args.include_responses else set())
    selectors = {
        v for c in settings.model_connections for v in (c.id.value, c.target.model_id)
    }
    if set(args.connection) - selectors:
        parser.error("unknown saved connection selector")
    connections = [
        c
        for c in settings.model_connections
        if c.target.model_id != "Qwen3.8-27B"
        and (
            (c.id.value in args.connection or c.target.model_id in args.connection)
            if args.connection
            else c.target.model_id in selected_models
        )
    ]
    if not connections:
        parser.error("no selected saved connections")
    output = (
        args.output
        or Path("scratch/image-interleaving-probe")
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    ).resolve()
    output.mkdir(parents=True, exist_ok=False)
    cards = fixtures(output / "images", args.repeat, args.seed)
    cases = [[make_case(name, batch) for name in CASES] for batch in cards]
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "effective_pulsara_home": str(require_pulsara_home()),
        "parameters": {
            k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()
        },
        "connections": [
            {
                "id": c.id.value,
                "model": c.target.model_id,
                "wire_api": c.target.wire_api.value,
                "base_url": c.base_url,
            }
            for c in connections
        ],
        "cards": cards,
        "cases": cases,
        "planned_requests_max": len(connections) * len(CASES) * args.repeat,
    }
    write_json(output / "manifest.json", manifest, scrub)
    print(
        f"Output: {output}; planned at most {manifest['planned_requests_max']} requests",
        flush=True,
    )
    if args.run:
        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        asyncio.run(run(args, settings, connections, cases, output, scrub))


if __name__ == "__main__":
    main()
