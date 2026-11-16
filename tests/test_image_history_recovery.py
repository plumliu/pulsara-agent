"""Complete image-history text and global immutable asset recovery."""
from time import monotonic
import pytest
from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.history_projection import canonical_history_message
from pulsara_agent.conversation_kernel.history_excerpt import excerpt_history, _weights
from pulsara_agent.conversation_kernel.session_content import SessionContentQuery, SessionQueryError
from pulsara_agent.conversation_kernel.prompt_storage import PostgresCanonicalImageReferenceReadPort
from pulsara_agent.llm.input import FrozenPromptContent, LLMTextPart, HISTORICAL_IMAGE_MARKER, PromptAnnotationPart, PromptAnnotationSource
from pulsara_agent.ports.session_content import SessionContentRange
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_conversation_fork import final, rows
from tests.support.history_projection import maintain_history
from tests.test_kernel_image_input_k2_postgres import _repository, _bound_session, _direct_intent, _image


def records(value):
    return [part.value for part in value.content if part.type=="object"]


@pytest.mark.postgres
def test_complete_image_history_search_and_cross_session_never_read_image_payloads(stage2_migrated_postgres_database,monkeypatch):
    repo=_repository(stage2_migrated_postgres_database)
    lease,runtime,_=_bound_session(repo)
    image=_image()
    originals=[FrozenPromptContent((LLMTextPart("needle-left"),image,LLMTextPart("右侧"),image)),FrozenPromptContent((image,image))]
    entries=[]
    for content in originals:
        intent,_=_direct_intent(repo,lease,runtime,content)
        final(repo,lease.guard,intent.turn_id,"saved answer needle-answer")
        entries.append(intent.entry_id)
    session=rows(repo,"SELECT * FROM pulsara_v3.sessions WHERE id=%s",(lease.guard.session_id,))[0]
    query=SessionContentQuery(repo,session_id=session["id"],memory_domain_id=session["memory_domain_id"])
    scope=SessionContentRange(session["latest_entry_sequence"])
    def invoke(operation,args,selected=scope):
        return query.invoke(operation,args,current_range=selected,deadline_monotonic=monotonic()+30)
    def forbid(*args,**kwargs):
        raise AssertionError("history text attempted image payload I/O")
    monkeypatch.setattr(PostgresCanonicalImageReferenceReadPort,"read_image",forbid)
    from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
    original_read=PostgresCanonicalBlobStore.read_exact_in_connection
    def metadata_only(connection,**kwargs):
        assert not kwargs.get("expected_media_type","").startswith("image/")
        return original_read(connection,**kwargs)
    monkeypatch.setattr(PostgresCanonicalBlobStore,"read_exact_in_connection",metadata_only)
    expected=f"needle-left\n{HISTORICAL_IMAGE_MARKER}\n右侧\n{HISTORICAL_IMAGE_MARKER}"
    result=records(invoke("read_session_content",{"target":{"kind":"SESSION_ENTRY","entry_id":entries[0]},"limit":1}))
    assert len(result)==1 and result[0]["text"]==expected
    assert records(invoke("read_session_content",{"target":{"kind":"SESSION_ENTRY","entry_id":entries[1]},"limit":1}))[0]["text"] == f"{HISTORICAL_IMAGE_MARKER}\n{HISTORICAL_IMAGE_MARKER}"
    assert records(invoke("read_session_content",{"target":{"kind":"SESSION","session_id":session["id"]},"limit":-1},SessionContentRange(0)))==[]
    with pytest.raises(SessionQueryError,match="not readable"):
        invoke("read_session_content",{"target":{"kind":"SESSION_ENTRY","entry_id":entries[0]},"limit":1},SessionContentRange(0))
    with repo.connection_provider.connection(lane=PostgresConnectionLane.MEMORY_QUERY,row_factory=dict_row,deadline_monotonic=monotonic()+30) as connection:
        row=connection.execute("SELECT * FROM pulsara_v3.transcript_entries WHERE id=%s",(entries[0],)).fetchone()
        projected=canonical_history_message(connection,row)
        assert projected["text"]==expected
        assert [field["text"] for field in projected["fields"]]==["needle-left","右侧"]
        assert image.content_digest not in str(projected)
        assert sum(_weights(projected))==len("needle-left")+2*4
    maintain_history(repo)
    target={"kind":"SESSION","session_id":session["id"]}
    hits=records(invoke("search_session_content",{"target":target,"query":"needle-left"}))
    assert hits and HISTORICAL_IMAGE_MARKER in str(hits)
    assert image.content_digest not in str(hits)
    for term in (image.content_digest,"历史图片"):
        assert records(invoke("search_session_content",{"target":target,"query":term}))==[]
    other,_,_=_bound_session(repo)
    cross=SessionContentQuery(repo,session_id=other.guard.session_id,memory_domain_id=session["memory_domain_id"])
    cross_value=cross.invoke("read_session_content",{"target":{"kind":"SESSION_ENTRY","entry_id":entries[0]},"limit":1},current_range=SessionContentRange(0),deadline_monotonic=monotonic()+30)
    assert records(cross_value)[0]["text"]==expected
    monkeypatch.undo()
    assert PostgresCanonicalImageReferenceReadPort(repo.connection_provider).read_image(image_ref=image.image_ref,maximum_encoded_bytes=1<<20,deadline_monotonic=monotonic()+30)==image


@pytest.mark.postgres
def test_annotation_near_body_quota_remains_searchable_with_image(stage2_migrated_postgres_database):
    repo=_repository(stage2_migrated_postgres_database)
    lease,runtime,_=_bound_session(repo)
    source,_=_direct_intent(repo,lease,runtime,FrozenPromptContent.text("source"))
    quote="a"*4056
    source_entry=final(repo,lease.guard,source.turn_id,quote)
    intent,_=_direct_intent(repo,lease,runtime,FrozenPromptContent((PromptAnnotationPart(quote=quote,source=PromptAnnotationSource(source_entry,0,len(quote)),comment="needle "),_image())))
    final(repo,lease.guard,intent.turn_id,"saved answer")
    session=rows(repo,"SELECT * FROM pulsara_v3.sessions WHERE id=%s",(lease.guard.session_id,))[0]
    maintain_history(repo)
    query=SessionContentQuery(repo,session_id=session["id"],memory_domain_id=session["memory_domain_id"])
    hits=records(query.invoke("search_session_content",{"target":{"kind":"SESSION","session_id":session["id"]},"query":"needle"},current_range=SessionContentRange(session["latest_entry_sequence"]),deadline_monotonic=monotonic()+30))
    assert hits
    text="\n".join(hit["text"] for hit in hits)
    assert quote in text and "needle " in text and HISTORICAL_IMAGE_MARKER in text


def test_excerpt_image_markers_are_whole_and_bounded():
    chunk = HISTORICAL_IMAGE_MARKER + "\npayload "
    text = "needle " + chunk * 100
    start = len("needle ")
    message = {
        "entry_id": 'entry:ik465kijafixxa6kpdisy6g4vu',
        "sequence": 1,
        "text": text,
        "annotated": False,
        "region": "Q1",
        "body_ranges": [[0, start]]
        + [
            [
                start + i * len(chunk) + len(HISTORICAL_IMAGE_MARKER) + 1,
                start + (i + 1) * len(chunk),
            ]
            for i in range(100)
        ],
        "atomic_ranges": [
            [
                start + i * len(chunk),
                start + i * len(chunk) + len(HISTORICAL_IMAGE_MARKER),
            ]
            for i in range(100)
        ],
    }
    document = {
        "messages": [message],
        "body": "needle",
        "positions": [{"start": 0, "end": 6, "message": 0, "display_start": 0}],
    }
    result = excerpt_history(document, ([[0, 6]],), budget=30)
    assert result
    shown = result[0]["text"]
    assert len(shown) < len(text)
    assert "历史图片" not in shown.replace(HISTORICAL_IMAGE_MARKER, "")
