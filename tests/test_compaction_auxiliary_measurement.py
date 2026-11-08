"""Auxiliary history measurement cannot bypass executable input contracts."""

import asyncio
from dataclasses import replace
from io import BytesIO
from time import monotonic

from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.provider_dispatch import (
    PreparedProviderWireCandidate,
    project_structured_installed_append,
)
from pulsara_agent.llm.errors import ModelTargetCapabilityMismatch
from pulsara_agent.llm.input import LLMTextPart
from pulsara_agent.llm.request import ProviderInputUsageAnchor
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    ContextSourceKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
)
from pulsara_agent.model_input.continuity import NoNewTriggerAnchor
from tests.support.images import bound_test_image
from tests.support.model_config import test_model_runtime as model_runtime_fixture
from tests.test_catalog_final_wire_resources import _prepare_case
from tests.test_round3_structured_model_input_compiler import _append_frontier, _candidate
from tests.test_stage2_direct_model import _continuity_candidate, _prepared_execution


_APIS = ("openai_chat_completions", "openai_responses")


@pytest.mark.parametrize("api", _APIS)
def test_auxiliary_image_meter_does_not_authorize_unsupported_model_execution(api, monkeypatch):
    port = DirectKernelModelPort(
        model_runtime=model_runtime_fixture(
            api_key="fixture-secret", base_url="https://example.invalid/v1",
            model_id="test-pro", wire_api=api, input_modalities=("text",),
        )
    )
    output = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(output, "PNG")
    image = bound_test_image("image/png", output.getvalue(), 7, 5)
    item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.USER,
        "entry:x6eevt4jzgsks3d5pdcognxexm", 1,
        "turn:klmmazz67vfgsfsvg6ceqgdsfu",
        (LLMTextPart("Original history image"), image),
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
    )
    # The fixture deliberately owns a raw image plan. The actual meter and
    # preflight below both run with the real production capability validator.
    with monkeypatch.context() as setup:
        setup.setattr(
            "pulsara_agent.conversation_kernel.direct_model.validate_model_message_content_for_call",
            lambda **_: None,
        )
        request, _ = _prepared_execution(port, canonical_items=(item,))
    try:
        prepared = request.prepared_call
        kwargs = dict(
            call=prepared.call, compile_binding=prepared.compile_binding,
            native_projection_set=prepared.native_projection_set,
            semantic_input=request.compiled_input, replay_hydration=None,
        )
        measurement = port.freeze_wire_measurement(
            **kwargs, validate_content_capability=False,
        )
        assert measurement.quote.final_wire_visual_image_tokens > 0
        assert measurement.discard_materialization_to_quote().final_wire_utf8_bytes > len(image.immutable_bytes)
        with pytest.raises(ModelTargetCapabilityMismatch, match="image input"):
            port.freeze_wire_measurement(**kwargs)
        owner, candidate = _continuity_candidate(request)
        with pytest.raises(ModelTargetCapabilityMismatch, match="image input"):
            port.preflight_execution(
                request, append_candidate=candidate,
                install_authority=owner.install_authority,
            )
        assert owner.current_cohort(candidate.scope) is None
    finally:
        request.surface_borrow.close()


@pytest.mark.parametrize("api", _APIS)
def test_independent_auxiliary_source_preserves_same_wire_usage_anchor(api):
    catalog = _candidate(ContextSourceKind.SKILL_CATALOG, ("Current skill catalog", ""))
    dispatch, candidate, installed, _ = _prepare_case(
        api, catalog, initial_catalog=catalog, budget=100_000,
    )
    owner = dispatch._continuity
    anchor_plan = installed.wire_input_plan
    call = candidate.call
    anchor = ProviderInputUsageAnchor(
        model_call_id=call.resolved_model_call_id,
        epoch_nonce=installed.epoch_nonce, epoch_revision=installed.epoch_revision,
        connection_id=call.binding.connection_id.value,
        target=call.target.fact,
        route_wire_profile_fingerprint=anchor_plan.route_wire_profile_fingerprint,
        reported_model_id="accepted-alias", reported_input_tokens=11,
        raw_input_tokens=anchor_plan.quote.raw_final_wire_estimated_input_tokens,
        materialization=anchor_plan.materialization,
    )
    owner.observe_usage_anchor(
        installed.scope, epoch_nonce=installed.epoch_nonce,
        epoch_revision=installed.epoch_revision, reported_model_id=anchor.reported_model_id,
        anchor=anchor,
    )
    auxiliary = owner.freeze_auxiliary_planning_input(
        scope=installed.scope,
        canonical_frontier=_append_frontier(candidate.compile_request),
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    projection = project_structured_installed_append(
        dispatch._compiler, candidate.compile_request,
        planning=auxiliary, deadline_monotonic=monotonic() + 30,
    )
    raw_candidate = PreparedProviderWireCandidate(
        canonical_read=candidate.canonical_read,
        semantic_input=projection.projected_input,
        prepared_call=candidate.prepared_call,
        native_projection_set=candidate.native_projection_set,
    )

    async def measure(usage_anchor):
        return await dispatch._freeze_candidate_wire_measurement(
            raw_candidate, deadline=monotonic() + 30,
            usage_anchor=usage_anchor, validate_content_capability=False,
        )

    measurement = asyncio.run(measure(owner.current_usage_anchor(installed.scope)))
    quote = measurement.discard_materialization_to_quote()
    assert quote.raw_final_wire_estimated_input_tokens == anchor.raw_input_tokens
    assert quote.budget_source == "reported_input_anchor"
    assert quote.budget_input_tokens == 11
    assert quote.estimated_suffix_tokens == 0
    assert quote.anchor_model_call_id == anchor.model_call_id
    assert auxiliary.predecessor_view is installed
    assert owner.current_view(installed.scope) is installed
    # Disabling capability admission only permits raw history measurement. It
    # does not relax an exact-prefix anchor's negative raw-delta invariant.
    with pytest.raises(ValueError, match="negative suffix"):
        asyncio.run(measure(replace(anchor, raw_input_tokens=anchor.raw_input_tokens + 1)))
    assert owner.current_usage_anchor(installed.scope) is anchor
    assert owner.current_view(installed.scope) is installed
