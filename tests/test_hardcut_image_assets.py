"""Global image assets and the shared blob storage boundary."""

from hashlib import sha256
from io import BytesIO

import psycopg
from psycopg.rows import dict_row
import pytest
from PIL import Image

from pulsara_agent.conversation_kernel.blob import (
    PostgresCanonicalBlobStore,
    _delete_orphans_in_connection,
    materialize_content_in_connection,
)
from pulsara_agent.conversation_kernel.content_parts import (
    ContentPartDraft,
    read_content_parts,
    write_content_parts,
)
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.prompt_storage import (
    CanonicalImageReferenceResourceExceeded,
    CanonicalImageReferenceUnavailable,
    content_parts_to_prompt,
    publish_prompt_images,
    prompt_to_content_drafts,
    read_image_reference_in_connection,
)
from pulsara_agent.conversation_kernel.repository_errors import (
    ConversationKernelConflict,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart, LLMTextPart


def _image() -> LLMImagePart:
    buffer = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(buffer, "PNG")
    return LLMImagePart("image/png", buffer.getvalue(), 7, 5)


def test_identical_images_have_one_identity_with_repeated_occurrences():
    first = freeze_canonical_prompt(
        FrozenPromptContent((LLMTextPart("A\n"), _image(), _image()))
    )
    second = freeze_canonical_prompt(FrozenPromptContent((_image(),)))
    assert (
        first.image_occurrences[0].image.image_ref
        == second.image_occurrences[0].image.image_ref
    )
    assert len(first.image_occurrences) == 2
    assert [part.type for part in prompt_to_content_drafts(first)] == [
        "text",
        "image",
        "image",
    ]


@pytest.fixture
def asset_connection(stage2_migrated_postgres_database):
    with psycopg.connect(
        stage2_migrated_postgres_database.admin_dsn, row_factory=dict_row
    ) as connection:
        yield connection
        connection.rollback()


def _owners(connection):
    """Install a valid imported prefix, without creating execution authority."""
    workspace, session, history, entry, assistant = (
        random_id(kind)
        for kind in ("workspace", "session", "history", "entry", "entry")
    )
    connection.execute(
        """INSERT INTO pulsara_v3.workspaces
           (memory_domain_id,id,workspace_kind,workspace_root,workspace_label)
           VALUES ('assets',%s,'project',%s,'assets')""",
        (workspace, f"/tmp/{workspace}"),
    )
    connection.execute(
        """INSERT INTO pulsara_v3.sessions(id,workspace_id,memory_domain_id,lifecycle,writer_generation)
           VALUES (%s,%s,'assets','OPEN',1)""",
        (session, workspace),
    )
    connection.execute(
        """INSERT INTO pulsara_v3.imported_history_groups
           (id,session_id,workspace_id,status,final_entry_id,accepted_at)
           VALUES (%s,%s,%s,'COMPLETED',%s,clock_timestamp())""",
        (history, session, workspace, assistant),
    )
    for sequence, identity, kind in (
        (1, entry, "USER_MESSAGE"),
        (2, assistant, "ASSISTANT_MESSAGE"),
    ):
        connection.execute(
            """INSERT INTO pulsara_v3.transcript_entries
               (id,origin_entry_id,session_id,workspace_id,entry_owner_kind,
                imported_history_group_id,entry_sequence,entry_kind,provider_input_through_sequence)
               VALUES (%s,%s,%s,%s,'IMPORTED_HISTORY',%s,%s,%s,%s)""",
            (
                identity,
                identity,
                session,
                workspace,
                history,
                sequence,
                kind,
                1 if sequence == 2 else None,
            ),
        )
    connection.execute(
        """INSERT INTO pulsara_v3.session_context_genesis
           (session_id,workspace_id,anchor_entry_id,model_call_binding,base_kind,source_through_sequence)
           VALUES (%s,%s,%s,'{"connection_id":"test","reasoning":null}', 'FULL_HISTORY',0)""",
        (session, workspace, assistant),
    )
    return workspace, session, entry, assistant


@pytest.mark.postgres
def test_asset_read_requires_only_existing_valid_identity(asset_connection):
    connection = asset_connection
    prompt = freeze_canonical_prompt(FrozenPromptContent((_image(),)))
    (image_ref,) = publish_prompt_images(connection, prompt=prompt)
    # The asset is readable before any entry, queue, or visualization exists.
    image = read_image_reference_in_connection(connection, image_ref=image_ref)
    assert image == prompt.image_occurrences[0].image
    row = connection.execute(
        "SELECT workspace_id,width,height FROM pulsara_v3.blobs WHERE id=%s",
        (image_ref,),
    ).fetchone()
    assert row == {"workspace_id": None, "width": 7, "height": 5}
    with pytest.raises(CanonicalImageReferenceResourceExceeded):
        read_image_reference_in_connection(
            connection, image_ref=image_ref, maximum_encoded_bytes=1
        )
    with pytest.raises(CanonicalImageReferenceUnavailable):
        read_image_reference_in_connection(connection, image_ref=random_id("image"))


@pytest.mark.postgres
def test_cross_workspace_references_and_visualization_protect_shared_asset(
    asset_connection,
):
    connection = asset_connection
    first_workspace, first_session, first_entry, _ = _owners(connection)
    second_workspace, second_session, second_entry, assistant = _owners(connection)
    prompt = freeze_canonical_prompt(
        FrozenPromptContent((LLMTextPart("before\n"), _image(), _image()))
    )
    image_ref = publish_prompt_images(connection, prompt=prompt)[0]
    for workspace, session, entry in (
        (first_workspace, first_session, first_entry),
        (second_workspace, second_session, second_entry),
    ):
        write_content_parts(
            connection,
            session_id=session,
            workspace_id=workspace,
            entry_id=entry,
            parts=prompt_to_content_drafts(prompt),
            publisher=None,
        )
        parts = read_content_parts(connection, entry_id=entry)
        assert content_parts_to_prompt(connection, parts).content == prompt.content
    history = connection.execute(
        "SELECT imported_history_group_id FROM pulsara_v3.transcript_entries WHERE id=%s",
        (assistant,),
    ).fetchone()["imported_history_group_id"]
    connection.execute(
        """INSERT INTO pulsara_v3.assistant_visualizations
           (session_id,workspace_id,assistant_entry_id,ordinal,imported_history_group_id,
            source_filename,state,blob_id)
           VALUES (%s,%s,%s,0,%s,'image.png','READY',%s)""",
        (second_session, second_workspace, assistant, history, image_ref),
    )
    from pulsara_agent.conversation_kernel.visualization import saved_visualizations

    saved = saved_visualizations(connection, second_session, assistant)
    assert len(saved) == 1
    assert (saved[0]["image_ref"], saved[0]["width"], saved[0]["height"]) == (
        image_ref,
        7,
        5,
    )
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.execute("DELETE FROM pulsara_v3.sessions WHERE id=%s", (first_session,))
    connection.execute(
        "DELETE FROM pulsara_v3.workspaces WHERE id=%s", (first_workspace,)
    )
    assert image_ref not in _delete_orphans_in_connection(
        connection, grace_seconds=0, maximum_items=50
    )
    connection.execute(
        "DELETE FROM pulsara_v3.content_parts WHERE entry_id=%s", (second_entry,)
    )
    assert image_ref not in _delete_orphans_in_connection(
        connection, grace_seconds=0, maximum_items=50
    )
    connection.execute("DELETE FROM pulsara_v3.sessions WHERE id=%s", (second_session,))
    assert image_ref in _delete_orphans_in_connection(
        connection, grace_seconds=0, maximum_items=50
    )


@pytest.mark.postgres
def test_single_large_text_and_object_remain_complete_parts(asset_connection):
    connection = asset_connection
    workspace, session, entry, _ = _owners(connection)
    value = "a\n" * ((16 << 20) // 2 + 1)
    parts = (
        ContentPartDraft("text", value),
        ContentPartDraft("object", {"body": value, "end": [None, True, 3]}),
    )
    written = write_content_parts(
        connection,
        session_id=session,
        workspace_id=workspace,
        entry_id=entry,
        parts=parts,
        publisher=None,
    )
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    loaded = read_content_parts(connection, entry_id=entry)
    assert [(part.id, part.ordinal, part.type, part.value) for part in loaded] == [
        (part.id, part.ordinal, part.type, part.value) for part in written
    ]
    rows = connection.execute(
        "SELECT logical_size FROM pulsara_v3.blobs WHERE workspace_id=%s", (workspace,)
    ).fetchall()
    assert len(rows) == 2 and all(row["logical_size"] > 16 << 20 for row in rows)


@pytest.mark.postgres
def test_byte_integrity_and_decoded_dimensions_are_verified(asset_connection):
    connection = asset_connection
    prompt = freeze_canonical_prompt(FrozenPromptContent((_image(),)))
    (image_ref,) = publish_prompt_images(connection, prompt=prompt)
    connection.execute("UPDATE pulsara_v3.blobs SET width=8 WHERE id=%s", (image_ref,))
    with pytest.raises(ConversationKernelConflict, match="decoding or dimensions"):
        read_image_reference_in_connection(connection, image_ref=image_ref)
    connection.execute(
        "UPDATE pulsara_v3.blobs SET width=7,body=decode(repeat('00',logical_size::integer),'hex') WHERE id=%s",
        (image_ref,),
    )
    with pytest.raises(ConversationKernelConflict, match="integrity"):
        read_image_reference_in_connection(connection, image_ref=image_ref)


@pytest.mark.postgres
def test_ordinary_blobs_keep_workspace_ownership(asset_connection):
    connection = asset_connection
    first_workspace, *_ = _owners(connection)
    second_workspace, *_ = _owners(connection)
    value = b"same bytes" * 10_000
    first = materialize_content_in_connection(
        connection,
        workspace_id=first_workspace,
        content=value,
        media_type="text/plain",
        codec="utf-8",
    )
    second = materialize_content_in_connection(
        connection,
        workspace_id=second_workspace,
        content=value,
        media_type="text/plain",
        codec="utf-8",
    )
    assert first.blob_id != second.blob_id
    assert (
        PostgresCanonicalBlobStore.read_exact_in_connection(
            connection,
            blob_id=first.blob_id,
            expected_digest="sha256:" + sha256(value).hexdigest(),
            expected_size=len(value),
            expected_workspace_id=first_workspace,
        )
        == value
    )
    with pytest.raises(ConversationKernelConflict):
        PostgresCanonicalBlobStore.read_exact_in_connection(
            connection,
            blob_id=first.blob_id,
            expected_digest=first.digest,
            expected_size=first.size,
            expected_workspace_id=second_workspace,
        )


@pytest.mark.postgres
def test_prompt_confirmation_quotes_metadata_without_reading_image_bytes(
    asset_connection, monkeypatch
):
    from pulsara_agent.conversation_kernel.prompt_storage import (
        canonical_prompt_owner_is_exact,
        validate_canonical_prompt_owner_metadata,
    )

    connection = asset_connection
    workspace, session, entry, _ = _owners(connection)
    prompt = freeze_canonical_prompt(
        FrozenPromptContent((LLMTextPart("before\n"), _image(), LLMTextPart("after")))
    )
    publish_prompt_images(connection, prompt=prompt)
    write_content_parts(
        connection,
        session_id=session,
        workspace_id=workspace,
        entry_id=entry,
        parts=prompt_to_content_drafts(prompt),
    )

    def unexpected_payload_read(*args, **kwargs):
        raise AssertionError("metadata admission must not read image payloads")

    monkeypatch.setattr(
        PostgresCanonicalBlobStore, "read_exact_in_connection", unexpected_payload_read
    )
    row = {"session_id": session, "workspace_id": workspace}
    metadata = validate_canonical_prompt_owner_metadata(
        connection, row=row, transcript_entry_id=entry
    )
    assert metadata.parts[1].image_ref == prompt.image_occurrences[0].image.image_ref
    assert canonical_prompt_owner_is_exact(
        connection, row=row, transcript_entry_id=entry, expected=prompt
    )


def test_visualization_image_reference_preparation_uses_global_read_api():
    import asyncio
    from time import monotonic
    from pulsara_agent.conversation_kernel.visualization import (
        materialize_visualizations,
    )

    image = freeze_canonical_prompt(FrozenPromptContent((_image(),))).content.parts[0]

    class IO:
        async def run(self, operation, *args, **kwargs):
            return operation(*args, **kwargs)

    class Reader:
        def read_image(self, *, image_ref, maximum_encoded_bytes, deadline_monotonic):
            assert image_ref == image.image_ref
            assert maximum_encoded_bytes > len(image.immutable_bytes)
            assert deadline_monotonic > monotonic()
            return image

    result = asyncio.run(
        materialize_visualizations(
            f"```pulsara-viz\n{image.image_ref}\n```",
            lambda path: path,
            io=IO(),
            validator=None,
            image_reader=Reader(),
            deadline_monotonic=monotonic() + 30,
        )
    )
    assert len(result) == 1 and result[0].image is image
