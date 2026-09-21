"""Offline checks for experimental controls, not provider token formula tests."""

from pathlib import Path

from PIL import Image

from pulsara_agent.llm.model_catalog import ModelTargetKey, WireApi
from pulsara_agent.llm.model_connections import (
    ModelConnectionConfig,
    ModelConnectionId,
    ReasoningWireProfile,
)
from tools.probe_vision_tokens import (
    Case,
    PROMPT,
    compare_to_baseline,
    make_fixtures,
    make_payload,
    usage_fields,
    write_summary,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet


def test_lossless_compression_control_preserves_pixels(tmp_path: Path) -> None:
    fixtures = make_fixtures(
        [Case("compressed", 128, 128), Case("raw", 128, 128, compression=0)],
        tmp_path / "images",
    )
    with Image.open(fixtures["compressed"]["path"]) as compressed:
        with Image.open(fixtures["raw"]["path"]) as raw:
            assert compressed.tobytes() == raw.tobytes()
    assert fixtures["compressed"]["file_bytes"] < fixtures["raw"]["file_bytes"]


def test_both_wire_shapes_preserve_repeated_images_and_baseline(tmp_path: Path) -> None:
    fixture = make_fixtures([Case("repeat", 64, 64, copies=2)], tmp_path / "images")[
        "repeat"
    ]
    for wire, root, image_type in (
        (WireApi.OPENAI_CHAT_COMPLETIONS, "messages", "image_url"),
        (WireApi.OPENAI_RESPONSES, "input", "input_image"),
    ):
        connection = ModelConnectionConfig(
            ModelConnectionId.new(),
            ModelTargetKey("openai", wire, "test-model"),
            "https://example.invalid/v1",
            ReasoningWireProfile.CATALOG_STANDARD,
        )
        payload = make_payload(connection, fixture, "auto", 256)
        baseline = make_payload(connection, None, "auto", 256)
        parts = payload[root][0]["content"]
        assert parts[0]["text"] == PROMPT
        assert baseline[root][0]["content"] == parts[:1]
        assert len(parts) == 3
        assert parts[1] == parts[2]
        assert parts[1]["type"] == image_type


def test_missing_usage_is_unknown_and_cached_tokens_remain_in_total() -> None:
    assert usage_fields(None)["input_tokens"] is None
    assert usage_fields(
        {"prompt_tokens": 900, "prompt_tokens_details": {"cached_tokens": 800}}
    ) == {
        "input_tokens": 900,
        "output_tokens": None,
        "cached_tokens": 800,
        "reasoning_tokens": None,
    }


def test_baseline_delta_requires_matching_reported_model_and_provider() -> None:
    baseline = {
        "status": "ok",
        "input_tokens": 30,
        "reported_model": "a",
        "reported_provider": "route-a",
    }
    row = {**baseline, "input_tokens": 1030}
    compare_to_baseline(row, baseline)
    assert row["input_delta_vs_text_baseline"] == 1000
    row["reported_model"] = "b"
    compare_to_baseline(row, baseline)
    assert row["input_delta_vs_text_baseline"] is None
    row["reported_model"] = "a"
    row["reported_provider"] = "route-b"
    compare_to_baseline(row, baseline)
    assert row["input_delta_vs_text_baseline"] is None


def test_negative_input_difference_is_preserved_but_not_a_visual_cost() -> None:
    baseline = {"status": "ok", "input_tokens": 4000, "reported_model": "same-model"}
    row = {**baseline, "input_tokens": 1200}
    compare_to_baseline(row, baseline)
    assert row["raw_input_difference"] == -2800
    assert row["input_delta_vs_text_baseline"] is None
    assert row["comparison_issue"] == "negative_input_difference"


def test_multiturn_baseline_removes_only_images_and_summary_matches_layout(
    tmp_path: Path,
) -> None:
    fixture = make_fixtures([Case("three", 64, 64, copies=3)], tmp_path / "images")[
        "three"
    ]
    for wire, root, image_type in (
        (WireApi.OPENAI_CHAT_COMPLETIONS, "messages", "image_url"),
        (WireApi.OPENAI_RESPONSES, "input", "input_image"),
    ):
        connection = ModelConnectionConfig(
            ModelConnectionId.new(),
            ModelTargetKey("openai", wire, "test-model"),
            "https://example.invalid/v1",
            ReasoningWireProfile.CATALOG_STANDARD,
        )
        payload = make_payload(connection, fixture, "auto", 1024, turns=3)
        baseline = make_payload(connection, None, "auto", 1024, turns=3)
        assert [m["role"] for m in payload[root]] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
        ]
        assert (
            sum(
                p["type"] == image_type
                for m in payload[root]
                if m["role"] == "user"
                for p in m["content"]
            )
            == 3
        )
        for message in payload[root]:
            if message["role"] == "user":
                message["content"] = [
                    p for p in message["content"] if p["type"] != image_type
                ]
        assert payload == baseline
    common = {
        "connection_id": "test",
        "repeat": 1,
        "detail": "auto",
        "status": "ok",
        "reported_model": "test",
    }
    rows = [
        {
            **common,
            "case": "text_baseline",
            "copies": 0,
            "turns": 1,
            "input_tokens": 30,
        },
        {
            **common,
            "case": "text_baseline_turns3",
            "copies": 0,
            "turns": 3,
            "input_tokens": 120,
        },
        {**common, "case": "three", "copies": 3, "turns": 3, "input_tokens": 1620},
    ]
    write_summary(tmp_path, rows, ProcessCredentialScrubSet())
    assert rows[-1]["input_delta_vs_text_baseline"] == 1500
