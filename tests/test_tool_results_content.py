"""The complete public result is distinct from execution facts and native pages."""
from contextlib import contextmanager

import pytest

from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.conversation_kernel.tool_outputs import (
    PostgresToolOutputReadPort, ToolOutputProcessor, assemble_tool_business_value,
    project_tool_result_export,
)
from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_execution import ToolBusinessValue, thaw_tool_json_object, tool_json_values_equal
from pulsara_agent.ports.tool_output import ToolOutputContentError, parse_tool_output_target


def test_complete_value_freezes_fields_objects_and_order_without_erasing_null():
    fields = {"structuredContent": None, "meta": {"count": 3, "ratio": 1.25},
              "external": {"next": [1, False, None]}}
    body = {"nested": [3, None, {"value": "a\r\nb\n"}]}
    value = ToolBusinessValue(fields, (ResultPart("object", body), ResultPart("text", "\x00tail\r\n")))
    prepared = ToolOutputProcessor.prepare(business_value=value, result_facts={"revision": 7})
    fields["structuredContent"] = "changed"
    body["nested"].clear()
    public = assemble_tool_business_value(public_fields=prepared.public_fields,
        content_parts=prepared.content_parts, result_state="APPLICATION_ERROR")
    assert public == {"structuredContent": None, "meta": {"status": "ERROR", "count": 3, "ratio": 1.25},
        "external": {"next": [1, False, None]},
        "content": [{"type": "object", "value": {"nested": [3, None, {"value": "a\r\nb\n"}]}},
                    {"type": "text", "value": "\x00tail\r\n"}]}
    assert "missing" not in public
    assert prepared.result_facts == {"revision": 7}
    assert "revision" not in project_tool_result_export(public).decode()


@pytest.mark.parametrize("state,expected", [("SUCCESS", "SUCCESS"), ("APPLICATION_ERROR", "ERROR"),
    ("SYSTEM_ERROR", "ERROR"), ("PERMISSION_DENIED", "ERROR"), ("CANCELLED", "CANCELLED"),
    ("CANCELLED_BEFORE_DISPATCH", "CANCELLED")])
def test_normal_failed_cancelled_values_have_the_same_complete_assembly(state, expected):
    public = assemble_tool_business_value(public_fields={"structuredContent": {"failure": [False, 0]}},
        content_parts=(ResultPart("text", "actual outcome"),), result_state=state)
    assert public["meta"] == {"status": expected}
    assert public["structuredContent"] == {"failure": [False, 0]}
    assert public["content"] == [{"type": "text", "value": "actual outcome"}]


@pytest.mark.parametrize("fields", [{"content": []}, {"meta": {"status": "SUCCESS"}},
    {"observed_at_utc": "now"}, {"observation_duration_microseconds": 1}])
def test_business_fields_cannot_copy_authoritative_content_or_observation(fields):
    with pytest.raises(ValueError):
        ToolBusinessValue(fields, ())


def test_export_preserves_text_bytes_and_deterministic_structured_projection():
    assert project_tool_result_export("a\r\nb\n\x00") == b"a\r\nb\n\x00"
    image = {"kind": "IMAGE_REF", "image_ref": random_id("image")}
    value = assemble_tool_business_value(public_fields={"meta": {"count": 2}}, result_state="SUCCESS",
        content_parts=(ResultPart("text", "before\r\n"), ResultPart("image", image),
                       ResultPart("target", {"kind": "PATH", "path": "a\nb"}),
                       ResultPart("object", {"b": 2, "a": [1, None]}), ResultPart("text", "after")))
    exported = project_tool_result_export(value).decode()
    assert '"count": 2' in exported and '"status": "SUCCESS"' in exported
    assert exported.index("before\r\n") < exported.index(image["image_ref"]) < exported.index('"path":"a\\nb"')
    assert '{\n  "a": [\n    1,\n    null\n  ],\n  "b": 2\n}\nafter' in exported


def test_minimal_result_target_rejects_session_next_and_non_result_handles():
    target = {"kind": "SESSION_ENTRY", "entry_id": random_id("entry")}
    assert parse_tool_output_target(target) == target
    for invalid in [{**target, "session_id": random_id("session")}, {"kind": "NEXT"},
                    {"kind": "PATH", "path": "file"}, {**target, "entry_id": random_id("result")}]:
        with pytest.raises((ValueError, TypeError)):
            parse_tool_output_target(invalid)


class Connection:
    def __init__(self, row):
        self.row, self.calls = row, []

    def execute(self, query, arguments):
        self.calls.append((query, arguments))
        return type("Result", (), {"fetchone": lambda _: self.row})()


class Provider:
    def __init__(self, connection):
        self.current = connection

    @contextmanager
    def connection(self, **_):
        yield self.current


def test_result_reader_requires_actual_accepted_result_and_keeps_internal_facts_private(monkeypatch):
    session, workspace, entry = (random_id(kind) for kind in ("session", "workspace", "entry"))
    row = {"session_id": session, "result_entry_id": entry, "result_state": "SUCCESS",
           "public_fields": {"structuredContent": None}, "output_unavailability_reason": None,
           "result_facts": {"canonical_path": "/private/actual", "file_mtime_ns": 123}}
    connection = Connection(row)
    monkeypatch.setattr("pulsara_agent.conversation_kernel.tool_outputs.read_content_parts",
                        lambda *_args, **_kwargs: ())
    reader = PostgresToolOutputReadPort(Provider(connection), session_id=session, workspace_id=workspace)
    target = {"kind": "SESSION_ENTRY", "entry_id": entry}
    view = reader.read_result(target)
    assert thaw_tool_json_object(view.value) == {"structuredContent": None, "meta": {"status": "SUCCESS"}, "content": []}
    assert connection.calls[0][1] == (session, workspace, entry)
    assert view.target == target
    row["output_unavailability_reason"] = "BODY_PUBLICATION_UNCONFIRMED"
    with pytest.raises(ToolOutputContentError, match="UNCONFIRMED"):
        reader.read_result(target)
    connection.row = None
    with pytest.raises(KeyError):
        reader.read_result(target)


def test_export_preserves_ecmascript_surrogate_code_units_in_json_values():
    import json
    value = assemble_tool_business_value(public_fields={"data": "\ud800", "ordinary": "中文"},
        content_parts=(ResultPart("object", {"value": "\ud800", "ordinary": "中文"}),), result_state="ERROR")
    exported = project_tool_result_export(value).decode("utf-8")
    decoder = json.JSONDecoder()
    fields, end = decoder.raw_decode(exported)
    part, _ = decoder.raw_decode(exported[end:].lstrip())
    assert fields["data"] == "\ud800" and fields["ordinary"] == "中文"
    assert part == {"value": "\ud800", "ordinary": "中文"}


@pytest.mark.parametrize("left,right", [
    (True, 1), (False, 0), (1, 1.25), (None, "null"),
    ({}, {"optional": None}), ([1, 2], [2, 1]),
    ({"nested": [True]}, {"nested": [1]}), ("\ud800", "\\ud800"),
    (10**30, 10**30 + 1), (1e30, 10**30 + 1),
])
def test_json_confirmation_distinguishes_types_presence_order_and_actual_code_units(left, right):
    assert not tool_json_values_equal(left, right)


def test_json_confirmation_uses_utf16_semantics_for_values_and_object_keys():
    original = {"\ud83d\ude00": ["\ud83d\ude00", "\ud800", "\x00", True, 1, 1.0], "\ue000": None}
    decoded = {"\ue000": None, "😀": ["😀", "\ud800", "\x00", True, 1, 1.0]}
    assert tool_json_values_equal(original, decoded)


@pytest.mark.parametrize("left,right", [(1, 1.0), (-0.0, 0), (1e20, 10**20), (1e30, 10**30)])
def test_json_confirmation_preserves_number_meaning_across_jsonb_spelling(left, right):
    assert tool_json_values_equal({"nested": [left]}, {"nested": [right]})
