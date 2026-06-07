"""Minimal CLI entrypoint for the Pulsara backend."""

from __future__ import annotations

import argparse
import json

from pulsara_agent import __version__
from pulsara_agent.memory.archive import InMemoryArchiveStore
from pulsara_agent.memory.graph import InMemoryGraphStore
from pulsara_agent.memory.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.ontology import memory
from pulsara_agent.settings import PulsaraSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulsara")
    parser.add_argument("--version", action="store_true", help="Print Pulsara version.")

    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("demo-ledger", help="Create and print a demo evidence ledger.")
    config_check = subcommands.add_parser(
        "config-check",
        help="Load Pulsara configuration from environment variables.",
    )
    config_check.add_argument(
        "--prefix",
        default="PULSARA",
        help="Environment variable prefix. Defaults to PULSARA.",
    )
    config_check.add_argument(
        "--env-file",
        default=None,
        help="Load configuration from a .env file before reading the environment.",
    )
    config_check.add_argument(
        "--override-env",
        action="store_true",
        help="Let values from --env-file override existing environment variables.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.version:
        print(__version__)
        return

    if args.command == "demo-ledger":
        graph = InMemoryGraphStore()
        archive = InMemoryArchiveStore()
        gate = MemoryWriteGate()
        ledger = ExecutionEvidenceLedger(graph=graph, archive=archive, gate=gate)
        result = ledger.record_tool_result(
            turn_id="turn:demo/001",
            tool_name="search_files",
            status=memory.ToolExecutionStatus.SUCCESS,
            input_summary="Search for JSON-LD flattening",
            output="Found JSON-LD flattening in memory graph conversion.",
            scope="ctx:demo",
        )
        evidence = ledger.create_evidence_from_tool_result(
            result.tool_result_id,
            statement="The tool result found a JSON-LD flattening concern.",
            scope="ctx:demo",
        )
        claim = ledger.submit_claim(
            statement="Pulsara should preserve JSON-LD semantics before optimizing recall.",
            scope="ctx:demo",
            evidence_ids=[evidence.evidence_id],
            source_authority=memory.SourceAuthority.TOOL_RESULT,
            verification_status=memory.VerificationStatus.TOOL_VERIFIED,
        )
        print(json.dumps({"tool_result": result.to_dict(), "evidence": evidence.to_dict(), "claim": claim.to_dict()}, indent=2))
        return

    if args.command == "config-check":
        try:
            if args.env_file:
                settings = PulsaraSettings.from_env_file(
                    args.env_file,
                    prefix=args.prefix,
                    override=args.override_env,
                )
            else:
                settings = PulsaraSettings.from_env(prefix=args.prefix)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(settings.redacted_dict(), indent=2))
        return

    parser.print_help()
