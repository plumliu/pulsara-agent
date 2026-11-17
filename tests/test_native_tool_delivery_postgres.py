"""Real complete-result, frozen native page, NEXT and history integration."""
from datetime import datetime, timezone
from io import BytesIO
from time import monotonic

from PIL import Image
from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.content_parts import ResultPart, read_content_parts
from pulsara_agent.conversation_kernel.native_tool_delivery import NativeResultPosition, render_native_tool_page
from pulsara_agent.conversation_kernel.repository import AssistantToolCallBlock, ConversationKernelRepository, build_prepared_tool_result_acceptance
from pulsara_agent.conversation_kernel.tool_outputs import (
    PostgresToolOutputReadPort, prepare_native_tool_observation, project_tool_result_export,
    restore_native_tool_observation,
)
from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_execution import ToolBusinessValue, thaw_tool_json_object
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.images import bound_test_image
from tests.support.postgres import verified_postgres_provider
from tests.test_hardcut_entry_parts import open_turn

pytestmark = pytest.mark.postgres


@pytest.fixture
def repo(stage2_migrated_postgres_database):
    return ConversationKernelRepository(verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn))


def save(repo, lease, turn, *, value, arguments=None, source=None, page=None, images=(), state="SUCCESS", tool_name="read_file"):
    cut = repo.prepare_provider_input_cut(lease.guard, turn_id=turn, deadline_monotonic=monotonic()+30)
    assistant, call = random_id("entry"), random_id("call")
    repo.commit_assistant_message(lease.guard, cut=cut, entry_id=assistant,
        blocks=(AssistantToolCallBlock(random_id("block"),call,tool_name,freeze_json(arguments or {"target":{"kind":"PATH","path":"result.txt"}})),),
        complete_turn=False, occurred_at=datetime.now(timezone.utc), actor_id="model:test", deadline_monotonic=monotonic()+30)
    with repo.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row, deadline_monotonic=monotonic()+30) as connection:
        row = connection.execute("SELECT workspace_id,permission_snapshot_fingerprint FROM pulsara_v3.turns WHERE id=%s",(turn,)).fetchone()
    attempt = repo.accept_tool_attempt(lease.guard,attempt_id=random_id("attempt"),assistant_entry_id=assistant,
        tool_call_id=call,authorization_kind="policy",authorization_reference="allow",actor_kind="runtime",actor_id="test",
        remote_idempotency_key=None,retry_of_attempt_id=None,permission_snapshot_fingerprint=row["permission_snapshot_fingerprint"],
        occurred_at=datetime.now(timezone.utc),deadline_monotonic=monotonic()+30)
    candidate = build_prepared_tool_result_acceptance(guard=lease.guard, workspace_id=row["workspace_id"],
        result_id=random_id("result"),result_entry_id=random_id("entry"),turn_id=turn,assistant_entry_id=assistant,
        tool_call_id=call,attempt_id=attempt.attempt_id,result_state=state,
        public_fields=value.public_fields,content_parts=value.content,
        source_result_entry_id=source,native_projection=None,result_facts={} if page is None else {"native_start":page.range.start.value()},pending_images=images,
        observed_at=datetime.now(timezone.utc),observation_duration_microseconds=1234,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,trusted_tool_reported_duration_microseconds=None,actor_id=tool_name)
    accepted = repo.accept_tool_result(lease.guard,candidate=candidate,deadline_monotonic=monotonic()+30)
    assert repo.confirm_tool_result_winner(lease.guard,candidate=candidate,deadline_monotonic=monotonic()+30) == accepted
    return candidate


def freeze_page(repo, guard, *, result_entry_id, page):
    from pulsara_agent.model_input.contracts import CompiledToolResultDecision
    decision=CompiledToolResultDecision(source_entry_fingerprint="unit-observation",current_turn=True,
        result_entry_id=result_entry_id,source_result_entry_id=page.source_entry_id,
        native_projection=freeze_json(page.projection_facts()))
    repo.freeze_native_tool_page_decisions(guard,decisions=(decision,),deadline_monotonic=monotonic()+30)


def admit_page(repo,guard,**kwargs):
    # These storage tests explicitly simulate a successfully admitted logical
    # page. The compiler/install suite verifies the actual admission boundary.
    page=prepare_native_tool_observation(repo,guard,**kwargs)
    freeze_page(repo,guard,result_entry_id=kwargs["result_entry_id"],page=page)
    return page


def stored(repo, entry):
    with repo.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,row_factory=dict_row,deadline_monotonic=monotonic()+30) as connection:
        row = connection.execute("SELECT * FROM pulsara_v3.tool_results WHERE result_entry_id=%s",(entry,)).fetchone()
        parts = read_content_parts(connection,entry_id=entry)
        page, text = restore_native_tool_observation(connection,row)
    return row, parts, page, text


def test_native_first_page_next_export_and_exact_history_never_copy_page_body(repo, monkeypatch):
    lease,intent,_,_ = open_turn(repo,text="native result\n")
    raw = "a\r\nb\n"*20000
    source = save(repo,lease,intent.turn_id,value=ToolBusinessValue(
        {"meta":{"total_lines":40000,"file_size":len(raw.encode()),"had_utf8_bom":False},"target":{"kind":"PATH","path":"result.txt"}},
        (ResultPart("text",raw),)))
    first = admit_page(repo,lease.guard,workspace_id=source.workspace_id,result_entry_id=source.result_entry_id,deadline_monotonic=monotonic()+30)
    assert first.next is not None and first.estimated_tokens <= 12000
    assert first.range.start == NativeResultPosition(0)
    row,parts,restored,text = stored(repo,source.result_entry_id)
    assert restored.body == first.body and restored.next == first.next
    assert row["source_result_entry_id"] == source.result_entry_id and len(parts)==1
    assert "1|a\r\n2|b\n" in first.body and "tool_result:" in text and "observed_at_utc:" in text
    # A future page chooser must not enlarge any already frozen observation.
    monkeypatch.setattr("pulsara_agent.conversation_kernel.native_tool_delivery.NATIVE_TOOL_PAGE_TOKENS",1000)
    again = admit_page(repo,lease.guard,workspace_id=source.workspace_id,result_entry_id=source.result_entry_id,deadline_monotonic=monotonic()+30)
    assert again.body == first.body and again.range == first.range and again.next == first.next
    port = PostgresToolOutputReadPort(repo.connection_provider,session_id=source.session_id,workspace_id=source.workspace_id)
    continuation = port.read_native_page(first.next)
    next_page = port.select_native_page(continuation.source,position=continuation.position)
    observed = save(repo,lease,intent.turn_id,value=ToolBusinessValue(),arguments={"target":dict(first.next)},
        source=source.result_entry_id,page=next_page)
    freeze_page(repo,lease.guard,result_entry_id=observed.result_entry_id,page=next_page)
    next_row,next_parts,replayed,next_text = stored(repo,observed.result_entry_id)
    assert next_row["public_fields"] == {} and next_parts == ()
    assert replayed.range.start == first.range.end and replayed.body == next_page.body
    assert replayed.source_entry_id == source.result_entry_id and replayed.next == next_page.next
    assert '"entry_id":"'+source.result_entry_id+'"' in next_text
    assert '"entry_id":"'+observed.result_entry_id+'"' not in next_text
    assert next_text == render_native_tool_page(next_page,observed_at=next_row["observed_at"],duration_microseconds=1234,continuation=True)
    with pytest.raises(RuntimeError,match="continuation"):
        port.read_result({"kind":"SESSION_ENTRY","entry_id":observed.result_entry_id})
    complete = port.read_result({"kind":"SESSION_ENTRY","entry_id":source.result_entry_id}).value
    assert complete["content"][0]["value"] == raw
    exported = project_tool_result_export(complete)
    assert exported.endswith(raw.encode()) and b"NEXT" not in exported and b"observed_at" not in exported
    assert repo.confirm_tool_result_winner(lease.guard,candidate=source,deadline_monotonic=monotonic()+30) is not None


def test_failed_long_object_has_frozen_pages_and_js_value_stays_complete(repo):
    lease,intent,_,_ = open_turn(repo,text="failed object\n")
    object_value = {"huge":"中"*32000,"next":{"external_page":2},"typed":[None,False,2.5]}
    source = save(repo,lease,intent.turn_id,state="APPLICATION_ERROR",value=ToolBusinessValue({},(ResultPart("object",object_value),)))
    page = admit_page(repo,lease.guard,workspace_id=source.workspace_id,result_entry_id=source.result_entry_id,deadline_monotonic=monotonic()+30)
    assert page.status == "ERROR" and page.next is not None and "[JSON 正文片段]" in page.body
    complete = PostgresToolOutputReadPort(repo.connection_provider,session_id=source.session_id,workspace_id=source.workspace_id).read_result({"kind":"SESSION_ENTRY","entry_id":source.result_entry_id})
    assert thaw_tool_json_object(complete.value)["content"][0]["value"] == object_value
    _,_,restored,_ = stored(repo,source.result_entry_id)
    assert restored.body == page.body and restored.range == page.range


def test_pending_images_publish_atomically_and_capability_projection_preserves_business(repo):
    lease,intent,_,_ = open_turn(repo,text="image source\n")
    output = BytesIO()
    Image.new("RGB",(19,23),(13,37,71)).save(output,format="PNG")
    asset = bound_test_image("image/png",output.getvalue(),19,23)
    source = save(repo,lease,intent.turn_id,images=(asset,),value=ToolBusinessValue({},(
        ResultPart("text","before"),ResultPart("image",{"kind":"IMAGE_REF","image_ref":asset.image_ref}),ResultPart("text","after"))))
    page = admit_page(repo,lease.guard,workspace_id=source.workspace_id,result_entry_id=source.result_entry_id,
        supports_images=False,deadline_monotonic=monotonic()+30)
    assert page.status == "ERROR" and page.images == () and page.next is None
    _,_,restored,text = stored(repo,source.result_entry_id)
    assert restored.body == page.body and "当前模型不支持图片输入，未交付实际图片。" in text
    complete = PostgresToolOutputReadPort(repo.connection_provider,session_id=source.session_id,workspace_id=source.workspace_id).read_result({"kind":"SESSION_ENTRY","entry_id":source.result_entry_id}).value
    assert complete["meta"]["status"] == "SUCCESS" and complete["content"][1]["type"] == "image"
    assert "当前模型不支持图片" not in project_tool_result_export(complete).decode()


def test_projection_slot_is_single_assignment_and_does_not_change_business_result(repo):
    lease,intent,_,_ = open_turn(repo,text="projection slot\n")
    source = save(repo,lease,intent.turn_id,value=ToolBusinessValue({},(ResultPart("text","small"),)))
    page = admit_page(repo,lease.guard,workspace_id=source.workspace_id,result_entry_id=source.result_entry_id,deadline_monotonic=monotonic()+30)
    freeze_page(repo,lease.guard,result_entry_id=source.result_entry_id,page=page)
    from pulsara_agent.conversation_kernel.native_tool_delivery import next_tool_result_target
    changed = {**page.projection_facts(),"end":[0,1], "next":next_tool_result_target(
        session_id=source.session_id,entry_id=source.result_entry_id,position=NativeResultPosition(0,1))}
    with pytest.raises(RuntimeError,match="frozen differently"):
        from dataclasses import replace
        changed_page=replace(page,range=type(page.range).parse({key:changed[key] for key in ("start","end","image_errors")}),next=changed["next"])
        freeze_page(repo,lease.guard,result_entry_id=source.result_entry_id,page=changed_page)


def test_provider_read_and_lowering_use_only_frozen_page_with_associated_image(repo):
    from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
    from pulsara_agent.model_input.contracts import FrozenProviderInputItemKind, ToolAttachmentSource, ToolAttachmentSourceMember
    from pulsara_agent.model_input.lowering import lower_canonical_item
    from pulsara_agent.model_input.compiler import _tool_attachment_message
    from pulsara_agent.llm.input import LLMImagePart
    lease,intent,_,_ = open_turn(repo,text="provider page\n")
    out=BytesIO()
    Image.new("RGB",(7,5),(13,37,71)).save(out,format="PNG")
    asset=bound_test_image("image/png",out.getvalue(),7,5)
    source=save(repo,lease,intent.turn_id,images=(asset,),value=ToolBusinessValue({},(
        ResultPart("text","before"),ResultPart("image",{"kind":"IMAGE_REF","image_ref":asset.image_ref}),ResultPart("text","after"))))
    page=admit_page(repo,lease.guard,workspace_id=source.workspace_id,result_entry_id=source.result_entry_id,deadline_monotonic=monotonic()+30)
    cut=repo.prepare_provider_input_cut(lease.guard,turn_id=intent.turn_id,deadline_monotonic=monotonic()+30)
    snapshot=CanonicalProviderInputReader(repo.connection_provider).read_frozen_snapshot(cut,deadline_monotonic=monotonic()+30)
    item=next(item for item in snapshot.items if item.item_kind is FrozenProviderInputItemKind.TOOL_RESULT)
    lowered=lower_canonical_item(item)
    message=lowered.message
    assert message.tool_call_id==source.tool_call_id
    assert message.content[0].text==item.tool_result_body_text
    assert "pulsara_tool_result" not in message.content[0].text
    assert "图片引用：" in message.content[0].text and "tool_result:" in message.content[0].text
    assert [part for part in item.content if isinstance(part,LLMImagePart)]==list(page.images)
    attachment=_tool_attachment_message(ToolAttachmentSource(source.assistant_entry_id,(ToolAttachmentSourceMember(0,item),)))
    assert attachment.content[0].text.find(source.tool_call_id)>=0
    assert attachment.content[0].text.find(asset.image_ref)>=0
    assert attachment.content[1]==asset


def test_native_install_range_confirms_lost_commit_ack_without_rewriting_or_reexecuting(repo, monkeypatch):
    lease, intent, _, _ = open_turn(repo, text="native lost acknowledgement")
    source = save(repo, lease, intent.turn_id,
        value=ToolBusinessValue(content=(ResultPart("text", "already accepted"),)))
    page = prepare_native_tool_observation(repo,lease.guard,workspace_id=source.workspace_id,
        result_entry_id=source.result_entry_id,deadline_monotonic=monotonic()+30)
    transaction = repo._writer_transaction
    calls = []

    class LoseAck:
        def __init__(self, *args, **kwargs):
            self.inner = transaction(*args, **kwargs)
        def __enter__(self):
            calls.append("write")
            return self.inner.__enter__()
        def __exit__(self, kind, value, traceback):
            self.inner.__exit__(kind, value, traceback)
            if kind is None:
                raise TimeoutError("native commit acknowledgement lost")

    monkeypatch.setattr(repo,"_writer_transaction",LoseAck)
    freeze_page(repo,lease.guard,result_entry_id=source.result_entry_id,page=page)
    row,parts,replayed,_ = stored(repo,source.result_entry_id)
    assert calls == ["write"]
    assert row["native_projection"] == page.projection_facts()
    assert parts[0].value == "already accepted" and replayed.body == page.body
