from __future__ import annotations

import asyncio
from types import SimpleNamespace
import traceback

import pytest

from pulsara_agent.capability.management_form import (
    CapabilityFormValues,
    PendingCapabilityForm,
)
from pulsara_agent.capability.mcp_management import McpSecretMutation
from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.interaction import KernelInteractionCoordinator
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import SessionLiveControlOwner
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.mcp_credentials import McpCredentialBinding, McpCredentialOwner
from pulsara_agent.primitives.context import freeze_json, thaw_json
from pulsara_agent.primitives.run_permission import (
    PermissionMode,
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)


class NoCanonicalWrites:
    def __getattr__(self, name):
        raise AssertionError(
            f"pre-admission capability form accessed repository: {name}"
        )


def setup():
    live = SessionLiveControlOwner(session_id="session:form")
    coordinator = KernelInteractionCoordinator(
        repository=NoCanonicalWrites(),
        guard=HostWriterGuard("session:form", 1, "host:1"),
        live_control=live,
        live_bus=LiveAgentEventBus(),
        io_owner=KernelSessionIO(),
    )
    permission = build_run_permission_snapshot(
        snapshot_id="permission:form",
        requested_mode=PermissionMode.READ_ONLY,
        effective_mode=PermissionMode.READ_ONLY,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )
    return coordinator, live, permission


async def open_form(coordinator, permission, prepare):
    waiter = asyncio.create_task(
        coordinator.request_capability_form(
            turn_id="turn:form",
            assistant_entry_id="entry:form",
            tool_call_id="call:form",
            permission_snapshot=permission,
            form=PendingCapabilityForm(
                freeze_json({"action": "ADD_LOCAL_MCP"}), "配置 MCP", prepare
            ),
        )
    )
    await asyncio.sleep(0)
    return waiter


def guard(live):
    snapshot = live.current_snapshot()
    return dict(
        attachment_id="browser:1",
        interaction_id=snapshot.current_interaction.interaction_id,
        expected_owner_epoch=snapshot.owner_epoch,
        expected_live_revision=snapshot.revision,
    )


def test_form_requires_controller_without_attempt():
    async def run():
        coordinator, live, permission = setup()

        async def prepare(_):
            pytest.fail("no-controller form must not parse input")

        result = await (await open_form(coordinator, permission, prepare))
        assert result.reference == "interaction:no-controller"
        assert result.attempt_id is None and result.capability_submission is None
        assert live.current_snapshot().current_interaction is None
        await coordinator.aclose()

    asyncio.run(run())


def test_form_secret_transfers_once_without_live_or_canonical_publication():
    async def run():
        coordinator, live, permission = setup()
        await coordinator.attach_controller("browser:1")
        binding = McpCredentialBinding(
            McpCredentialOwner("local", "user", "test"), "bearer"
        )

        async def prepare(value):
            return CapabilityFormValues(
                freeze_json({"server_id": "test"}),
                (McpSecretMutation(binding, value["key"]),),
            )

        waiter = await open_form(coordinator, permission, prepare)
        expected = guard(live)
        snapshot = live.current_snapshot()
        assert snapshot.current_interaction.interaction_kind == "CAPABILITY_FORM"
        assert snapshot.current_interaction.public_options == ("SUBMIT", "CANCEL")
        assert thaw_json(coordinator.current_capability_form(**expected)) == {
            "action": "ADD_LOCAL_MCP"
        }
        await coordinator.resolve_capability_form(
            **expected, decision="SUBMIT", submission={"key": "fixture-private-value"}
        )
        result = await waiter
        assert result.decision == "SUBMIT" and result.attempt_id is None
        assert "fixture-private-value" not in repr(result)
        assert "fixture-private-value" not in repr(live._ring)
        assert live.current_snapshot().current_interaction is None
        values = result.capability_submission.take()
        assert values.secret_changes[0].value == "fixture-private-value"
        with pytest.raises(RuntimeError, match="already consumed"):
            result.capability_submission.take()
        with pytest.raises(ConversationKernelConflict):
            await coordinator.resolve_capability_form(
                **expected, decision="SUBMIT", submission={}
            )
        await coordinator.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("decision", ["ALLOW", "DENY"])
def test_boolean_confirmation_cannot_settle_capability_form(decision):
    async def run():
        coordinator, live, permission = setup()
        await coordinator.attach_controller("browser:1")

        async def prepare(_):
            pytest.fail("boolean decision must not parse the form")

        waiter = await open_form(coordinator, permission, prepare)
        expected = guard(live)
        with pytest.raises(ConversationKernelConflict, match="SUBMIT or CANCEL"):
            await coordinator.resolve_tool_interaction(
                expected_writer_generation=1,
                **{k: v for k, v in expected.items() if k != "attachment_id"},
                command_id="command:form",
                actor_id="browser:1",
                decision=decision,
            )
        assert not waiter.done()
        await coordinator.resolve_capability_form(**expected, decision="CANCEL")
        result = await waiter
        assert result.decision == "CANCEL" and result.capability_submission is None
        await coordinator.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "wrong_field,wrong_value",
    [
        ("attachment_id", "browser:foreign"),
        ("interaction_id", "interaction:foreign"),
        ("expected_live_revision", 999),
        ("expected_owner_epoch", 999),
    ],
)
def test_form_rejects_foreign_or_stale_controller(wrong_field, wrong_value):
    async def run():
        coordinator, live, permission = setup()
        await coordinator.attach_controller("browser:1")

        async def prepare(_):
            pytest.fail("stale controller must not submit")

        waiter = await open_form(coordinator, permission, prepare)
        expected = guard(live)
        expected[wrong_field] = wrong_value
        with pytest.raises(ConversationKernelConflict):
            coordinator.current_capability_form(**expected)
        with pytest.raises(ConversationKernelConflict):
            await coordinator.resolve_capability_form(
                **expected, decision="SUBMIT", submission={}
            )
        await coordinator.aclose()
        result = await waiter
        assert result.capability_submission is None and result.attempt_id is None

    asyncio.run(run())


def test_invalid_submission_keeps_same_form_for_correction():
    async def run():
        coordinator, live, permission = setup()
        await coordinator.attach_controller("browser:1")

        async def prepare(value):
            if not value.get("ready"):
                raise ValueError("missing public field")
            return CapabilityFormValues(freeze_json(value))

        waiter = await open_form(coordinator, permission, prepare)
        expected = guard(live)
        with pytest.raises(ValueError, match="missing public field"):
            await coordinator.resolve_capability_form(
                **expected, decision="SUBMIT", submission={}
            )
        assert guard(live) == expected and not waiter.done()
        await coordinator.resolve_capability_form(
            **expected, decision="SUBMIT", submission={"ready": True}
        )
        assert (await waiter).capability_submission.take().public_fields == freeze_json(
            {"ready": True}
        )
        await coordinator.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "interruption", ["detach", "detach_reattach", "close", "cancel_turn"]
)
def test_form_validation_race_cannot_accept_after_owner_loss(interruption):
    async def run():
        coordinator, live, permission = setup()
        await coordinator.attach_controller("browser:1")
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(_):
            entered.set()
            await release.wait()
            return CapabilityFormValues(freeze_json({"ready": True}))

        waiter = await open_form(coordinator, permission, prepare)
        submission = asyncio.create_task(
            coordinator.resolve_capability_form(
                **guard(live),
                decision="SUBMIT",
                submission={},
            )
        )
        await entered.wait()
        if interruption == "cancel_turn":
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            ending = None
        else:
            ending = asyncio.create_task(
                coordinator.aclose()
                if interruption == "close"
                else coordinator.controller_detached("browser:1")
            )
            await asyncio.sleep(0)
            if interruption == "close":
                assert not ending.done()
            else:
                assert ending.done()
                assert not coordinator.is_current_controller("browser:1")
                assert coordinator._pending.capability_cancelled
                assert not submission.done()
            if interruption == "detach_reattach":
                assert await coordinator.attach_controller("browser:1")
        release.set()
        await submission
        if ending is not None:
            await ending
            result = await waiter
            assert result.capability_submission is None and result.attempt_id is None
        assert live.current_snapshot().current_interaction is None
        await coordinator.aclose()

    asyncio.run(run())


def test_capability_and_boolean_confirmation_share_one_fifo_slot():
    async def run():
        coordinator, live, permission = setup()
        await coordinator.attach_controller("browser:1")

        async def prepare(_):
            return CapabilityFormValues(freeze_json({}))

        waiter = await open_form(coordinator, permission, prepare)
        ordinary = asyncio.create_task(
            coordinator.request_tool_confirmation(
                turn_id="turn:ordinary",
                assistant_entry_id="entry:ordinary",
                tool_call_id="call:ordinary",
                tool_name="terminal",
                permission_snapshot=permission,
            )
        )
        await asyncio.sleep(0)
        assert (
            live.current_snapshot().current_interaction.interaction_kind
            == "CAPABILITY_FORM"
        )
        await coordinator.resolve_capability_form(**guard(live), decision="CANCEL")
        assert (await waiter).decision == "CANCEL"
        assert (
            live.current_snapshot().current_interaction.interaction_kind
            == "TOOL_CONFIRMATION"
        )
        await coordinator.aclose()
        assert (await ordinary).reference == "interaction:host-closing"

    asyncio.run(run())


def test_browser_form_uses_current_host_without_secret_protocol_frames():
    from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
    from pulsara_agent.web_app.protocol_client import ProtocolBridgeError

    async def run():
        coordinator, live, permission = setup()
        await coordinator.attach_controller("browser:1")

        async def prepare(value):
            if value.get("key"):
                raise ValueError(f"validator accidentally echoed {value['key']}")
            return CapabilityFormValues(freeze_json({}))

        waiter = await open_form(coordinator, permission, prepare)
        session = SimpleNamespace(
            read_capability_form=coordinator.current_capability_form,
            resolve_capability_form=coordinator.resolve_capability_form,
        )

        def get_session(host_id):
            assert host_id == "host:form"
            return session

        bridge = LocalBrowserBridge(
            sessions=SimpleNamespace(session_by_host_id=get_session),
            protocol_server=None,
        )
        bridge._connections["connection:form"] = SimpleNamespace(
            is_open=True,
            role="controller",
            host_session_id="host:form",
            controller=SimpleNamespace(attachment_id="browser:1"),
        )
        body = {k: v for k, v in guard(live).items() if k != "attachment_id"}
        assert await bridge.capability_form("connection:form", body, submit=False) == {
            "form": {"action": "ADD_LOCAL_MCP"},
        }
        with pytest.raises(ProtocolBridgeError) as error:
            await bridge.capability_form(
                "connection:form",
                {
                    **body,
                    "decision": "SUBMIT",
                    "submission": {"key": "private-http-only-value"},
                },
                submit=True,
            )
        assert error.value.code == "CAPABILITY_FORM_INVALID"
        assert "private-http-only-value" not in "".join(
            traceback.format_exception(error.value)
        )
        assert not waiter.done()
        bridge._connections["connection:form"].role = "observer"
        with pytest.raises(ProtocolBridgeError, match="CONTROLLER_REQUIRED"):
            await bridge.capability_form("connection:form", body, submit=False)
        bridge._connections["connection:form"].role = "controller"
        assert await bridge.capability_form(
            "connection:form",
            {
                **body,
                "decision": "SUBMIT",
                "submission": {},
            },
            submit=True,
        ) == {"submitted": True}
        assert (await waiter).decision == "SUBMIT"
        # The fake controller intentionally has no Protocol request method:
        # no secret-bearing frame or canonical interaction command was sent.
        await coordinator.aclose()

    asyncio.run(run())
