"""Run the real desktop UI against a disposable DB, recording timing edges.

Browser DOM observations use Date.now(), the same machine's wall clock as these
timestamps. Touch output/playwright/suggestion-latency/stop to cleanly stop.
"""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, time_ns
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.ephemeral import (
    SessionEphemeralProducts,
    SUGGESTION_PROMPT,
)
from pulsara_agent.desktop_app.application import DesktopApplication
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ReasoningEffortSelection,
)
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.ports.live_agent_event import TextDeltaPayload, TextEndPayload
from pulsara_agent.ports.provider_stream import ProviderStreamTerminal
from pulsara_agent.settings import LocalSettingsStore, LocalPostgresConfig
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.tool_permission import default_permission_policy
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import _ReadOnlySettingsStore


async def run():
    home, saved = require_pulsara_home(), LocalSettingsStore().read()
    scrubber = secret_scrubber(saved)
    directory = Path("output/playwright/suggestion-latency").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stop = directory / "stop"
    if stop.exists():
        raise RuntimeError("Remove the previous stop marker before a new run")
    events = []
    info = {"prompt": SUGGESTION_PROMPT, "events": events, "sessions": []}

    def persist():
        (directory / "timing.json").write_text(
            scrubber.scrub_text(json.dumps(info, ensure_ascii=False, indent=2))
        )

    def event(kind, **values):
        events.append({"kind": kind, "time_ms": time_ns() / 1_000_000, **values})
        persist()

    class Execution:
        def __init__(self, delegate, tags):
            self.delegate, self.tags, self.first = delegate, tags, True

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        async def read_next(self):
            item = await self.delegate.read_next()
            if isinstance(item, TextDeltaPayload) and self.first:
                self.first = False
                event("first_text", **self.tags)
            if isinstance(item, TextEndPayload):
                event("text_end", text=item.final_text, **self.tags)
            if isinstance(item, ProviderStreamTerminal):
                event(
                    "provider_terminal", terminal=item.terminal_kind.value, **self.tags
                )
            return item

    class Borrow:
        def __init__(self, delegate):
            self.delegate, self.call = delegate, delegate.call

        def open_stream(self, *, context):
            tags = {
                "wire": self.call.target.fact.wire_api,
                "purpose": self.call.fact.purpose.value,
                "context_id": context.context_id,
            }
            event("provider_open", **tags)
            return Execution(self.delegate.open_stream(context=context), tags)

        def close(self):
            self.delegate.close()

    original_borrow = ModelRuntime.borrow_transport
    original_complete = SessionEphemeralProducts.completed
    original_generate = SessionEphemeralProducts._generate_suggestion

    def completed(owner, origin_turn_id, result):
        source = getattr(result, "final_source", None)
        event(
            "root_completed",
            session_id=owner._host.session_id,
            context_id=None if source is None else source.compiled_input.context_id,
        )
        return original_complete(owner, origin_turn_id, result)

    async def generated(owner, slot, source):
        try:
            return await original_generate(owner, slot, source)
        finally:
            event(
                "suggestion_settled",
                session_id=owner._host.session_id,
                context_id=source.compiled_input.context_id,
                state=slot.state,
                text=slot.text,
            )

    socket = home / "desktop" / "socket"
    root_admin = make_conninfo(
        host=str(socket), port=5432, dbname="pulsara", user="pulsara_admin"
    )
    name = "pulsara_latency_" + uuid4().hex[:12]
    with psycopg.connect(root_admin, autocommit=True) as conn:
        address, data_dir = conn.execute(
            "SELECT inet_server_addr(), current_setting('data_directory')"
        ).fetchone()
        assert (
            address is None
            and Path(data_dir).resolve() == (home / "desktop" / "postgres").resolve()
        )
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    admin = make_conninfo(
        host=str(socket), port=5432, dbname=name, user="pulsara_admin"
    )
    runtime_dsn = make_conninfo(
        host=str(socket), port=5432, dbname=name, user="pulsara_runtime"
    )
    previous_home = os.environ.get("PULSARA_HOME")
    app = None
    try:
        PostgresMigrationRunner(admin_dsn=admin, runtime_dsn=runtime_dsn).migrate(
            deadline_monotonic=monotonic() + 240
        )
        with TemporaryDirectory(prefix="pulsara-latency-") as temporary:
            os.environ["PULSARA_HOME"] = temporary
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            runtime = ModelRuntime.production(settings=settings, catalog=catalog)
            ModelRuntime.borrow_transport = lambda self, permit: Borrow(
                original_borrow(self, permit)
            )
            SessionEphemeralProducts.completed = completed
            SessionEphemeralProducts._generate_suggestion = generated
            app = DesktopApplication(
                settings=settings,
                catalog=catalog,
                model_runtime=runtime,
                permission_policy=default_permission_policy(intent="inspect"),
            )
            await app.start()
            for wire, label in [
                ("openai_chat_completions", "Chat 延迟测量"),
                ("openai_responses", "Responses 延迟测量"),
            ]:
                connection = next(
                    c
                    for c in saved.model_connections
                    if c.target.route_id == "deepseek"
                    and c.target.model_id == "deepseek-flash"
                    and c.target.wire_api.value == wire
                )
                handle = await app.sessions.create_session(
                    workspace_kind="quick",
                    model_call_binding=ModelCallBinding(
                        connection.id, ReasoningEffortSelection("high")
                    ),
                )
                await app.sessions.rename_session(handle.session_id, label)
                info["sessions"].append(
                    {"wire": wire, "id": handle.session_id, "title": label}
                )
            info["origin"] = app.origin
            persist()
            print(
                json.dumps(
                    {"origin": app.origin, "timing": str(directory / "timing.json")},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            while not stop.exists():
                await asyncio.sleep(0.2)
    finally:
        if app is not None:
            await app.aclose()
        ModelRuntime.borrow_transport = original_borrow
        SessionEphemeralProducts.completed = original_complete
        SessionEphemeralProducts._generate_suggestion = original_generate
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        with psycopg.connect(root_admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
        info["cleaned_up"] = True
        persist()


if __name__ == "__main__":
    asyncio.run(run())
