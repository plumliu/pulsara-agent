"""Inspectable local HTTP/SSE evidence through the real OpenAI SDK and adapters.

The server deliberately closes chunked SSE mid-response. This is a local fault
experiment with fixture credentials and model data, never a real upstream call.
The existing adapter test harness owns the HTTP fixture and request setup.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import logging
from pathlib import Path
from unittest.mock import patch

import openai

from pulsara_agent.ports.provider_stream import (
    ProviderGenerationRetry,
    ProviderNormalizedTerminalKind,
)
from tests.test_provider_stream_retry import (
    APIS, execution, partial, server, success,
)


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


class Logs(logging.Handler):
    def __init__(self, target):
        super().__init__()
        self.target = target

    def emit(self, record):
        self.target.append({"at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                            "level": record.levelname, "logger": record.name, "text": record.getMessage()})


def run(destination):
    if destination.exists():
        raise RuntimeError("evidence destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {"started_at": now(), "scope": "local controlled HTTP/SSE service + real SDK + production adapters",
              "upstream_provider": False, "retry_sleep": "injected async fast sleep; planned delay retained", "cases": []}
    original_close = openai.AsyncStream.close
    current = {}

    async def close(stream):
        item = {"started_at": now()}
        current["closes"].append(item)
        try:
            await original_close(stream)
            item["closed"] = True
        except BaseException as exc:
            item["error"] = {"type": type(exc).__name__, "text": str(exc)}
            raise
        finally:
            item["finished_at"] = now()

    try:
        with patch.object(openai.AsyncStream, "close", close):
            for api in APIS:
                for kind in ("text", "thinking", "tool", "exhausted"):
                    item = {"api": api, "case": kind, "started_at": now(), "logs": [], "closes": [], "normalized": [], "retry_observations": []}
                    current = item
                    report["cases"].append(item)
                    logger = logging.getLogger("pulsara_agent")
                    handler = Logs(item["logs"])
                    logger.addHandler(handler)
                    logger.setLevel(logging.DEBUG)
                    scripts = [{"events": partial(api, "text" if kind == "exhausted" else kind), "cut": True}]
                    if kind != "exhausted":
                        scripts.append({"events": success(api)})
                    item["server_response_scripts"] = scripts
                    try:
                        with server(scripts) as (endpoint, requests):
                            item["requests"] = requests
                            value, delays, _, _, _ = execution(api, endpoint, observer=item["retry_observations"].append)
                            item["planned_delays"] = delays
                            async def collect():
                                values = item["normalized"]
                                while (event := await value.read_next()) is not None:
                                    if isinstance(event, ProviderGenerationRetry):
                                        assert len(item["closes"]) == len(requests)
                                        assert all(row.get("closed") for row in item["closes"])
                                    values.append(event)
                                return values
                            normalized = asyncio.run(collect())
                            item.update(requests=requests, planned_delays=delays, normalized=normalized)
                            assert all(body == requests[0] for body in requests)
                            expected = 6 if kind == "exhausted" else 2
                            assert len(requests) == expected and len(item["closes"]) == expected
                            assert all(row.get("closed") for row in item["closes"])
                            assert len(delays) == expected - 1
                            terminal = normalized[-1]
                            if kind == "exhausted":
                                assert terminal.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
                                assert terminal.error.retry_summary.retry_count == 5
                                assert terminal.error.retry_summary.exhausted
                            else:
                                assert terminal.terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
                                replacement = next(i for i, event in enumerate(normalized) if isinstance(event, ProviderGenerationRetry))
                                assert "DISCARDED" not in str(normalized[replacement+1:])
                                assert "discarded-call" not in str(terminal.completed_replay_payload)
                                assert "ACCEPTED" in str(normalized[replacement+1:])
                            item["expected_attempts"] = expected
                            item["identical_wire_inputs"] = True
                            item["no_tool_dispatch"] = "adapter returns function descriptions only; no local tool owner was invoked"
                            item["status"] = "passed"
                    finally:
                        logger.removeHandler(handler)
                        item["finished_at"] = now()
                        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=encode)+"\n")
        report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", failure={"type": type(exc).__name__, "text": str(exc)})
        raise
    finally:
        report["finished_at"] = now()
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=encode)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
