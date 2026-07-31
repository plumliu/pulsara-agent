from __future__ import annotations

import asyncio

import pytest

from pulsara_agent.ports.mcp_elicitation import (
    McpConfirmedUrlLaunchAuthority,
    McpUrlLaunchDisposition,
    McpUrlLaunchOutcome,
)
from pulsara_agent.ports.mcp_secret import (
    McpElicitationAction,
    McpSealedElicitationResponseFactory,
    build_retryable_tool_call_payload,
)
from pulsara_agent.primitives.mcp_continuation import (
    McpContinuationBoundsFact,
    build_mcp_continuation_fact,
    default_mcp_continuation_bounds,
)
from pulsara_agent.runtime.mcp.browser import SystemMcpExternalBrowserPort
from pulsara_agent.runtime.mcp.elicitation_batch import (
    McpElicitationBatchState,
    build_mcp_elicitation_batch_owner,
)
from pulsara_agent.runtime.mcp.protocol import (
    McpClientInputRequiredLeg,
    McpElicitationUrlPolicyRejected,
    McpInputRequiredContractError,
    McpUnadvertisedInputRequest,
    lower_input_required_result,
    state_only_retry_delay,
)


def _mixed_input_leg() -> tuple[McpClientInputRequiredLeg, tuple[object, ...]]:
    retryable = build_retryable_tool_call_payload(
        tool_name="profile",
        arguments={"scope": "self"},
        source_method_schema_fingerprint="m" * 64,
    )
    leg, private_urls = lower_input_required_result(
        input_requests={
            "email": {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "Share an email address",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"email": {"type": "string"}},
                        "required": ["email"],
                        "additionalProperties": False,
                    },
                },
            },
            "login": {
                "method": "elicitation/create",
                "params": {
                    "mode": "url",
                    "message": "Continue in the browser",
                    "url": "https://example.com/authorize?state=private-canary",
                },
            },
        },
        request_state="opaque-round-state",
        leg_ordinal=1,
        retryable_payload=retryable,
        operation_deadline_monotonic=10_000.0,
        commitment_key_id="test-key",
        keyed_commitment=lambda _domain, _payload: "hmac-sha256:" + "a" * 64,
        elicitation_advertised=True,
        bounds=default_mcp_continuation_bounds(),
    )
    assert isinstance(leg, McpClientInputRequiredLeg)
    return leg, private_urls


def _bounds(**updates: int) -> McpContinuationBoundsFact:
    payload = default_mcp_continuation_bounds().model_dump(
        mode="python",
        exclude={"bounds_fingerprint"},
    )
    payload.update(updates)
    return build_mcp_continuation_fact(McpContinuationBoundsFact, **payload)


def test_mixed_elicitation_requires_exact_full_round_and_explicit_url_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        opened_urls: list[str] = []

        def _open(url: str, *, new: int, autoraise: bool) -> bool:
            assert new == 2
            assert autoraise
            opened_urls.append(url)
            return True

        monkeypatch.setattr(
            "pulsara_agent.runtime.mcp.browser.webbrowser.open", _open
        )
        leg, private_urls = _mixed_input_leg()
        browser = SystemMcpExternalBrowserPort()
        owner = build_mcp_elicitation_batch_owner(
            runtime_session_id="session:mixed",
            interaction_id="interaction:mixed",
            round_ordinal=1,
            request_set_fingerprint=leg.request_set_fingerprint,
            requests=leg.input_requests,
            private_url_payloads=private_urls,
            response_factory=McpSealedElicitationResponseFactory(
                commitment_key_id="test-key",
                commitment_key=b"k" * 32,
                bounds=default_mcp_continuation_bounds(),
            ),
            browser_port=browser,
        )

        assert owner.state is McpElicitationBatchState.COLLECTING
        assert opened_urls == []
        assert owner.exact_url_for_display(request_key="login").endswith(
            "state=private-canary"
        )
        assert opened_urls == []

        owner.submit_form(
            request_key="email",
            action=McpElicitationAction.ACCEPT,
            content_present=True,
            content={"email": "person@example.com"},
        )
        assert owner.frozen_resolution is None
        with pytest.raises(RuntimeError, match="not resolution-ready"):
            owner.begin_commit()

        outcome = await owner.launch_url(
            request_key="login",
            consent_receipt_fingerprint="consent" * 8,
        )
        assert outcome.disposition is McpUrlLaunchDisposition.LAUNCHED
        assert opened_urls == [
            "https://example.com/authorize?state=private-canary"
        ]
        assert owner.frozen_resolution is None

        owner.confirm_url_retry(request_key="login")
        frozen = owner.frozen_resolution
        assert frozen is not None
        assert frozen.ordered_request_keys == ("email", "login")
        assert owner.state is McpElicitationBatchState.RESOLUTION_READY
        assert owner.begin_commit() is frozen
        owner.confirm_commit("full")
        assert owner.state is McpElicitationBatchState.FULL
        await owner.drain(
            deadline_monotonic=asyncio.get_running_loop().time() + 1.0
        )
        owner.retire()
        assert owner.state is McpElicitationBatchState.RETIRED

    asyncio.run(run())


def test_input_required_state_only_schedule_and_unadvertised_method_are_closed() -> None:
    assert tuple(state_only_retry_delay(index) for index in range(1, 7)) == (
        0.05,
        0.1,
        0.2,
        0.25,
        0.25,
        0.25,
    )
    retryable = build_retryable_tool_call_payload(
        tool_name="lookup",
        arguments={},
        source_method_schema_fingerprint="m" * 64,
    )
    with pytest.raises(McpUnadvertisedInputRequest, match="not advertised"):
        lower_input_required_result(
            input_requests={
                "sample": {
                    "method": "sampling/createMessage",
                    "params": {},
                }
            },
            request_state=None,
            leg_ordinal=1,
            retryable_payload=retryable,
            operation_deadline_monotonic=10_000.0,
            commitment_key_id="test-key",
            keyed_commitment=lambda _domain, _payload: "hmac-sha256:" + "a" * 64,
            elicitation_advertised=True,
            bounds=default_mcp_continuation_bounds(),
        )


def test_url_launch_caller_cancellation_detaches_from_physical_owner() -> None:
    class BlockingBrowser:
        contract_fingerprint = "browser:test"

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        def register_owner(self, **_kwargs) -> None:
            return

        def release_owner(self, **_kwargs) -> None:
            return

        def exact_url_for_display(self, **_kwargs) -> str:
            return "https://example.com/authorize"

        async def launch(
            self,
            authority: McpConfirmedUrlLaunchAuthority,
        ) -> McpUrlLaunchOutcome:
            self.started.set()
            await self.release.wait()
            return McpUrlLaunchOutcome(
                disposition=McpUrlLaunchDisposition.LAUNCHED,
                physical_operation_id=f"launch:{authority.request_key}",
            )

    async def run() -> None:
        leg, private_urls = _mixed_input_leg()
        browser = BlockingBrowser()
        owner = build_mcp_elicitation_batch_owner(
            runtime_session_id="session:cancel",
            interaction_id="interaction:cancel",
            round_ordinal=1,
            request_set_fingerprint=leg.request_set_fingerprint,
            requests=leg.input_requests,
            private_url_payloads=private_urls,
            response_factory=McpSealedElicitationResponseFactory(
                commitment_key_id="test-key",
                commitment_key=b"k" * 32,
                bounds=default_mcp_continuation_bounds(),
            ),
            browser_port=browser,
        )
        waiter = asyncio.create_task(
            owner.launch_url(
                request_key="login",
                consent_receipt_fingerprint="consent" * 8,
            )
        )
        await browser.started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        browser.release.set()
        await owner.drain(
            deadline_monotonic=asyncio.get_running_loop().time() + 1.0
        )
        login_slot = next(
            slot for slot in owner.item_slots if slot.request.key == "login"
        )
        assert login_slot.state.value == "awaiting_url_retry"
        owner.confirm_url_retry(request_key="login")
        owner.retire()

    asyncio.run(run())


def test_continuation_bounds_are_enforced_at_lowering_and_round_freeze() -> None:
    retryable = build_retryable_tool_call_payload(
        tool_name="lookup",
        arguments={},
        source_method_schema_fingerprint="m" * 64,
    )
    common = {
        "request_state": None,
        "leg_ordinal": 1,
        "retryable_payload": retryable,
        "operation_deadline_monotonic": 10_000.0,
        "commitment_key_id": "test-key",
        "keyed_commitment": (
            lambda _domain, _payload: "hmac-sha256:" + "a" * 64
        ),
        "elicitation_advertised": True,
    }
    form_request = {
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "Value",
            "requestedSchema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        },
    }
    with pytest.raises(McpInputRequiredContractError, match="count"):
        lower_input_required_result(
            input_requests={"a": form_request, "b": form_request},
            bounds=_bounds(maximum_input_requests=1),
            **common,
        )
    with pytest.raises(McpInputRequiredContractError, match="event-safe"):
        lower_input_required_result(
            input_requests={
                "a": {
                    **form_request,
                    "params": {
                        **form_request["params"],
                        "message": "x" * 1024,
                    },
                }
            },
            bounds=_bounds(maximum_input_requests_event_bytes=256),
            **common,
        )
    with pytest.raises(McpElicitationUrlPolicyRejected, match="exceeds"):
        lower_input_required_result(
            input_requests={
                "url": {
                    "method": "elicitation/create",
                    "params": {
                        "mode": "url",
                        "message": "Open",
                        "url": "https://example.com/private",
                    },
                }
            },
            bounds=_bounds(maximum_private_url_utf8_bytes=16),
            **common,
        )

    leg, private_urls = _mixed_input_leg()
    owner = build_mcp_elicitation_batch_owner(
        runtime_session_id="session:bounded",
        interaction_id="interaction:bounded",
        round_ordinal=1,
        request_set_fingerprint=leg.request_set_fingerprint,
        requests=leg.input_requests,
        private_url_payloads=private_urls,
        response_factory=McpSealedElicitationResponseFactory(
            commitment_key_id="test-key",
            commitment_key=b"k" * 32,
            bounds=_bounds(maximum_current_round_response_bytes=128),
        ),
        browser_port=SystemMcpExternalBrowserPort(),
    )
    owner.decline_or_cancel_url(
        request_key="login",
        action=McpElicitationAction.DECLINE,
    )
    with pytest.raises(ValueError, match="current-round responses"):
        owner.submit_form(
            request_key="email",
            action=McpElicitationAction.ACCEPT,
            content_present=True,
            content={"email": "x" * 1024},
        )
    email_slot = next(
        slot for slot in owner.item_slots if slot.request.key == "email"
    )
    assert email_slot.response is None
    assert owner.frozen_resolution is None
    owner.retire()
