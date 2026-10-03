"""Diagnostic canonical history for prompt behavior probes, not recovery machinery.

The unknown-effect fixture admits a real root intent and exact tool attempt with
normal owners. The probe performs its harmless local effect, deliberately omits
the result, then interrupts the turn. It fabricates no provider-native replay.
"""

from datetime import datetime, timezone
from pathlib import Path
import shlex
import subprocess
from time import monotonic
from uuid import uuid4

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.repository import (
    AssistantToolCallBlock,
    build_prepared_root_turn_intent,
)
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane


async def seed_unknown_local_append(session, workspace: Path) -> dict[str, str]:
    """Create an interrupted once-only append with a real, inspectable effect."""
    marker = workspace / "append-once.txt"
    if marker.exists():
        raise ValueError("unknown-effect fixture requires a fresh marker path")
    command = f"printf 'DONE_ONCE\\n' >> {shlex.quote(str(marker))}"
    suffix = uuid4().hex
    turn_id = f"turn:prompt-unknown:{suffix}"
    guard, repository = session._lease.guard, session.repository
    intent = build_prepared_root_turn_intent(
        session_id=session.session_id,
        command_id=f"command:prompt-unknown:{suffix}",
        turn_id=turn_id,
        entry_id=f"entry:prompt-unknown:user:{suffix}",
        context_binding_revision_id=f"revision:prompt-unknown:{suffix}",
        permission_snapshot_id=f"permission:prompt-unknown:{suffix}",
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        canonical_prompt=freeze_canonical_prompt(FrozenPromptContent.text(
            f"Run {command!r} once to append one DONE_ONCE line, then verify completion."
        )),
        occurred_at=datetime.now(timezone.utc),
        actor_id="prompt-cognitive-fixture",
    )
    resolution = session._model_runtime.freeze_resolution_snapshot()
    candidate = repository.prepare_root_provider_input_candidate(
        guard, intent=intent, model_resolution_snapshot=resolution,
        deadline_monotonic=monotonic() + 60,
    )
    prepared = await session._runner._provider_dispatch.prepare_prospective_root_input(
        candidate=candidate,
        inherited_memory_use_policy=session._runner._root_memory_use_policy,
        deadline=monotonic() + 60,
    )
    try:
        repository.accept_root_turn_intent(
            guard, intent=intent, provider_input_admission=prepared.admission,
            model_resolution_snapshot=resolution, deadline_monotonic=monotonic() + 60,
        )
    finally:
        prepared.close()
    cut = repository.prepare_provider_input_cut(
        guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30,
    )
    entry_id, call_id = f"entry:prompt-unknown:assistant:{suffix}", f"call:prompt-unknown:{suffix}"
    repository.commit_assistant_message(
        guard, cut=cut, entry_id=entry_id,
        parent_content=InlineContent.from_bytes(b""),
        blocks=(AssistantToolCallBlock(
            f"block:prompt-unknown:{suffix}", call_id, "terminal",
            freeze_json({"command": command}),
        ),),
        occurred_at=datetime.now(timezone.utc), actor_id="prompt-cognitive-fixture",
        deadline_monotonic=monotonic() + 30,
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 30,
    ) as connection:
        permission = str(connection.execute(
            "SELECT permission_snapshot_fingerprint FROM pulsara_v3.turns "
            "WHERE session_id=%s AND id=%s", (session.session_id, turn_id),
        ).fetchone()[0])
    repository.accept_tool_attempt(
        guard, attempt_id=f"attempt:prompt-unknown:{suffix}",
        assistant_entry_id=entry_id, tool_call_id=call_id,
        authorization_kind="policy", authorization_reference="allow",
        actor_kind="runtime", actor_id="prompt-cognitive-fixture",
        remote_idempotency_key=None, retry_of_attempt_id=None,
        permission_snapshot_fingerprint=permission,
        occurred_at=datetime.now(timezone.utc), deadline_monotonic=monotonic() + 30,
    )
    # Controlled probe execution: a real local effect, with no accepted result.
    subprocess.run(["/bin/sh", "-c", command], cwd=workspace, check=True)
    if not repository.interrupt_turn(
        guard, turn_id=turn_id, reason="FOREGROUND_EXECUTION_INTERRUPTED",
        occurred_at=datetime.now(timezone.utc), actor_id="prompt-cognitive-fixture",
        deadline_monotonic=monotonic() + 30,
    ):
        raise RuntimeError("unknown-effect fixture could not interrupt its turn")
    return {"marker": str(marker), "command": command, "turn_id": turn_id}
