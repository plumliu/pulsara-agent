from __future__ import annotations

from hashlib import sha256

import pytest

from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    compaction_handoff_instruction,
    compaction_snapshot_canonical_expanded_bytes,
    freeze_compaction_summary_output,
)
from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMTextPart,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    CompactionActiveRequestLocation,
    CompactionContinuationMode,
    CompactionSnapshotCarrier,
    FrozenCompactionActiveRequest,
    FrozenProviderInputItemKind,
    FrozenRetainedHistoricalRequest,
)
from pulsara_agent.primitives.context import canonical_json_bytes


def _image(payload: bytes = b"validated-image") -> LLMImagePart:
    return LLMImagePart("image/png", payload, 1, 1)


def _mixed_content() -> FrozenPromptContent:
    return FrozenPromptContent(
        (LLMTextPart("before"), _image(), LLMTextPart("after"))
    )


def _idle_carrier(
    request: FrozenRetainedHistoricalRequest,
) -> CompactionSnapshotCarrier:
    return build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output(
            "earlier summary", maximum_utf8_bytes=1024
        ),
        recent_human_requests=(request,),
        continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER,
        active_request=None,
    )


@pytest.mark.parametrize(
    "active,origin",
    (
        (True, CanonicalInputOriginKind.SUBAGENT_OBJECTIVE),
        (False, CanonicalInputOriginKind.SUBAGENT_OBJECTIVE),
        (False, CanonicalInputOriginKind.USER_CONTROL_FEEDBACK),
    ),
)
def test_compaction_request_images_are_closed_to_human_origins(
    active: bool, origin: CanonicalInputOriginKind
) -> None:
    if active:
        with pytest.raises(ValueError, match="cannot carry image"):
            FrozenCompactionActiveRequest(
                entry_id="entry:active",
                entry_sequence=1,
                location=CompactionActiveRequestLocation.SNAPSHOT_EXACT,
                item_kind=FrozenProviderInputItemKind.USER,
                input_origin=origin,
                content=_mixed_content(),
            )
    else:
        with pytest.raises(ValueError, match="cannot carry image"):
            FrozenRetainedHistoricalRequest(
                item_kind=FrozenProviderInputItemKind.USER,
                input_origin=origin,
                content=_mixed_content(),
            )


def test_compaction_metadata_decoder_rejects_nonhuman_image_before_hydration() -> None:
    image = _image()
    raw = canonical_json_bytes(
        {
            "continuation": {
                "mode": CompactionContinuationMode.AWAIT_NEXT_USER.value,
                "instruction": compaction_handoff_instruction(
                    continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER,
                    active_request=None,
                ),
                "active_request": None,
            },
            "earlier_context_summary": "summary",
            "recent_human_requests": (),
            "retained_historical_requests": (
                {
                    "item_kind": FrozenProviderInputItemKind.USER.value,
                    "input_origin": CanonicalInputOriginKind.SUBAGENT_OBJECTIVE.value,
                    "content": {
                        "schema": "pulsara.prompt/v1",
                        "parts": (
                            {
                                "type": "image",
                                "digest": image.content_digest,
                                "encoded_bytes": len(image.immutable_bytes),
                                "media_type": image.media_type,
                                "width": image.width,
                                "height": image.height,
                            },
                        ),
                    },
                },
            ),
        }
    )

    with pytest.raises(ValueError, match="cannot carry image"):
        compaction_snapshot_canonical_expanded_bytes(raw)


def test_snapshot_canonical_expanded_bytes_counts_each_image_occurrence() -> None:
    image = _image()
    request = FrozenRetainedHistoricalRequest(
        item_kind=FrozenProviderInputItemKind.USER,
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        content=FrozenPromptContent((image, LLMTextPart("again"), image)),
    )
    carrier = _idle_carrier(request)

    assert carrier.canonical_expanded_bytes == (
        len(carrier.body) + 2 * len(image.immutable_bytes)
    )




def test_blob_exact_read_guards_body_projection_with_expected_metadata() -> None:
    class _Result:
        def fetchone(self):
            return {
                "workspace_id": "workspace:test",
                "logical_digest": "sha256:" + "0" * 64,
                "logical_size": 1024,
                "media_type": "image/png",
                "codec": "binary",
                "body": None,
            }

    class _Connection:
        sql = ""

        def execute(self, sql, parameters):
            del parameters
            self.sql = sql
            return _Result()

    connection = _Connection()
    expected = "sha256:" + sha256(b"small").hexdigest()
    with pytest.raises(ConversationKernelConflict, match="integrity mismatch"):
        PostgresCanonicalBlobStore.read_exact_in_connection(
            connection,
            blob_id="blob:test",
            expected_digest=expected,
            expected_size=5,
            expected_workspace_id="workspace:test",
            expected_media_type="image/png",
            expected_codec="binary",
        )
    assert "CASE WHEN logical_digest" in connection.sql
    assert "octet_length(body)" in connection.sql
