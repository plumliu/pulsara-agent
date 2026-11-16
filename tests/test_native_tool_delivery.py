"""Pages are ordered views of one frozen value, never new business executions."""
from io import BytesIO

from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.native_tool_delivery import (
    NATIVE_TOOL_PAGE_TOKENS, NativeResultPosition, next_tool_result_target,
    project_native_tool_page, resolve_tool_result_next,
)
from pulsara_agent.conversation_kernel.tool_outputs import assemble_tool_business_value, project_tool_result_export
from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.identity import random_id
from tests.support.images import bound_test_image


@pytest.fixture
def identities():
    return random_id("session"), random_id("entry")


def image(width, height):
    out = BytesIO()
    Image.new("RGB", (width, height), (13, 37, 71)).save(out, format="PNG")
    return bound_test_image("image/png", out.getvalue(), width, height)


def pages(identities, value, **kwargs):
    session, entry = identities
    position = NativeResultPosition(0)
    reader = kwargs.pop("image_reader", lambda _ref: None)
    while True:
        page = project_native_tool_page(session_id=session, entry_id=entry,
            value=value, image_reader=reader, start=position, **kwargs)
        yield page
        if page.next is None:
            break
        selected, position = resolve_tool_result_next(page.next, session_id=session)
        assert selected == entry


def test_long_text_and_external_next_keep_a_contiguous_prefix(identities):
    value = assemble_tool_business_value(public_fields={"external": {"next": {"page": 9}}},
        content_parts=(ResultPart("text", "a\r\nb\n" * 25000),), result_state="CANCELLED")
    selected = list(pages(identities, value))
    assert len(selected) > 2
    assert all(page.estimated_tokens <= NATIVE_TOOL_PAGE_TOKENS for page in selected)
    assert all(page.status == "CANCELLED" for page in selected)
    assert "".join(page.body for page in selected).encode() == project_tool_result_export(value)
    assert selected[-1].next is None
    for left, right in zip(selected, selected[1:]):
        assert left.range.end == right.range.start


def test_long_object_splits_its_deterministic_json_without_structural_shards(identities):
    value = assemble_tool_business_value(public_fields={}, content_parts=(
        ResultPart("object", {"large": "中"*40000, "other": [None, False, 2.5]}),), result_state="ERROR")
    selected = list(pages(identities, value))
    assert len(selected) > 3
    assert all(page.estimated_tokens <= 12000 for page in selected)
    assert all("[JSON 正文片段]" in page.body for page in selected)
    plain = "".join(page.body.replace("[JSON 正文片段]\n", "") for page in selected)
    assert plain.encode() == project_tool_result_export(value)
    assert all(page.status == "ERROR" for page in selected)


def test_target_is_indivisible_and_is_not_followed_or_reordered(identities):
    target = {"kind": "PATH", "path": "/file/" + "x"*2000}
    value = assemble_tool_business_value(public_fields={}, content_parts=(
        ResultPart("text", "a"*47000), ResultPart("target", target), ResultPart("text", "tail")), result_state="SUCCESS")
    selected = list(pages(identities, value))
    assert selected[0].range.end == NativeResultPosition(2)
    assert target["path"] not in selected[0].body
    assert target["path"] in selected[1].body and selected[1].body.endswith("tail")


def test_visual_budget_and_oversize_single_image_use_existing_v3(identities):
    first, second = image(1920, 1080), image(1920, 1080)
    images = {first.image_ref: first, second.image_ref: second}
    value = assemble_tool_business_value(public_fields={}, content_parts=(ResultPart("text", "中"*8000),
        ResultPart("image", {"kind":"IMAGE_REF","image_ref":first.image_ref}),
        ResultPart("image", {"kind":"IMAGE_REF","image_ref":second.image_ref}), ResultPart("text", "end")), result_state="SUCCESS")
    selected = []
    position = NativeResultPosition(0)
    while True:
        page = project_native_tool_page(session_id=identities[0], entry_id=identities[1], value=value,
            image_reader=images.__getitem__, start=position)
        selected.append(page)
        if page.next is None:
            break
        _, position = resolve_tool_result_next(page.next, session_id=identities[0])
    assert len(selected) == 2 and len(selected[0].images) == len(selected[1].images) == 1
    assert all(page.estimated_tokens <= 12000 for page in selected)
    giant = image(4096, 4096)
    value = assemble_tool_business_value(public_fields={}, content_parts=(ResultPart("image", {"kind":"IMAGE_REF","image_ref":giant.image_ref}),
        ResultPart("text", "after")), result_state="SUCCESS")
    start = NativeResultPosition(1)
    page = project_native_tool_page(session_id=identities[0], entry_id=identities[1], value=value,
        image_reader=lambda _ref: giant, start=start)
    assert page.estimated_tokens == 18908 and page.images == (giant,)
    assert "after" not in page.body and page.range.end == NativeResultPosition(2)


def test_unsupported_image_text_projection_advances_and_is_frozen(identities):
    saved = image(8, 9)
    value = assemble_tool_business_value(public_fields={}, content_parts=(
        ResultPart("text", "before"), ResultPart("image", {"kind":"IMAGE_REF","image_ref":saved.image_ref}),
        ResultPart("text", "after")), result_state="SUCCESS")
    def never_read(_ref):
        raise AssertionError("unsupported image capability does not load asset bytes")
    page = project_native_tool_page(session_id=identities[0], entry_id=identities[1], value=value,
        image_reader=never_read, image_dimensions=lambda _ref: (8,9), supports_images=False)
    assert page.status == "ERROR" and page.images == () and page.next is None
    assert page.body.index("before") < page.body.index("当前模型不支持图片输入，未交付实际图片。") < page.body.index("after")
    restored = project_native_tool_page(session_id=identities[0], entry_id=identities[1], value=value,
        image_reader=never_read, image_dimensions=lambda _ref: (8,9), frozen_range=page.range, frozen_next=page.next)
    assert restored.body == page.body and restored.range == page.range
    assert value["meta"]["status"] == "SUCCESS" and value["content"][1]["type"] == "image"


def test_next_is_authenticated_and_session_bound_without_turn_or_epoch_binding(identities):
    session, entry = identities
    target = next_tool_result_target(session_id=session, entry_id=entry, position=NativeResultPosition(4,123))
    assert set(target) == {"kind","token"}
    assert resolve_tool_result_next(target, session_id=session) == (entry, NativeResultPosition(4,123))
    for bad in [{**target,"session_id":session}, {**target,"token":target["token"][:-3]+"AAA"}, {"kind":"SESSION_ENTRY","entry_id":entry}]:
        with pytest.raises(ValueError):
            resolve_tool_result_next(bad, session_id=session)
    with pytest.raises(ValueError):
        resolve_tool_result_next(target, session_id=random_id("session"))


def test_native_file_line_numbers_use_frozen_total_and_lf_only(identities):
    value = assemble_tool_business_value(public_fields={"meta":{"total_lines":100}},
        content_parts=(ResultPart("text", "2|first\r\nsecond\rinside\n"),), result_state="SUCCESS")
    page = project_native_tool_page(session_id=identities[0], entry_id=identities[1], value=value,
        image_reader=lambda _:None, source_tool_name="read_file", source_arguments={"target":{"kind":"PATH","path":"file"},"offset":-2})
    assert "99|2|first\r\n100|second\rinside\n" in page.body
    assert value["content"][0]["value"] == "2|first\r\nsecond\rinside\n"
