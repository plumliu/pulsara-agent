"""Display-only metadata must survive archive without changing execution truth."""
from time import monotonic
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.conversation_kernel.host import _kernel_session_summary, _read_resumable_session_row
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.web_app.session_controller import LocalSessionController
from tests.test_conversation_fork import repo as repo, new_session, rows, turn
from tests.test_session_archive import archive


def rename(repo, sid, title, domain="u_local"):
    return repo.rename_session(session_id=sid, memory_domain_id=domain, title=title,
                               deadline_monotonic=monotonic() + 30)


def test_title_is_metadata_and_survives_archive_restore(repo):
    lease = new_session(repo)
    sid = lease.guard.session_id
    turn(repo, lease.guard, "history stays unchanged")
    def session():
        return rows(repo, "SELECT * FROM pulsara_v3.sessions WHERE id=%s", (sid,))[0]
    def transcript():
        return rows(repo, "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence", (sid,))
    before, messages = session(), transcript()
    assert rename(repo, sid, "  中文 标题 🐈  ") == "中文 标题 🐈"
    after = session()
    assert after.pop("title") == "中文 标题 🐈"
    before.pop("title")
    assert after == before
    assert transcript() == messages
    summary = _kernel_session_summary(_read_resumable_session_row(repo, sid, "u_local", False, monotonic() + 30))
    assert LocalSessionController._summary_payload(summary, True)["title"] == "中文 标题 🐈"
    assert LocalSessionController._summary_payload(replace(summary, title=None), False)["title"].startswith("会话 ")
    assert archive(repo, lease) == "ARCHIVED"
    archived = session()
    rename(repo, sid, "归档中的新名称")
    changed = session()
    assert changed.pop("title") == "归档中的新名称"
    archived.pop("title")
    assert changed == archived
    repo.unarchive_session(session_id=sid, memory_domain_id="u_local", deadline_monotonic=monotonic() + 30)
    assert session()["title"] == "归档中的新名称"
    assert transcript() == messages


def test_rename_live_session_does_not_require_idle_or_change_writer(repo):
    lease = new_session(repo)
    sid = lease.guard.session_id
    turn(repo, lease.guard, "still running", finish=False)
    before = rows(repo, "SELECT * FROM pulsara_v3.sessions WHERE id=%s", (sid,))[0]
    rename(repo, sid, "运行中的会话")
    after = rows(repo, "SELECT * FROM pulsara_v3.sessions WHERE id=%s", (sid,))[0]
    assert after == {**before, "title": "运行中的会话"}


@pytest.mark.parametrize("title", [None, 1, "", "   ", "a\nb", "a\tb", "a\x00b", "a\x7fb", "a\u2028b"])
def test_invalid_title_never_changes_row(repo, title):
    sid = new_session(repo).guard.session_id
    with pytest.raises(ValueError):
        rename(repo, sid, title)
    assert rows(repo, "SELECT title FROM pulsara_v3.sessions WHERE id=%s", (sid,)) == [{"title": None}]


def test_title_does_not_cross_domain_or_recreate_deleted_sessions(repo):
    sid = new_session(repo).guard.session_id
    with pytest.raises(KeyError):
        rename(repo, sid, "wrong domain", "other")
    with repo.connection_provider.connection(lane=PostgresConnectionLane.HOST_CONTROL, deadline_monotonic=monotonic() + 30) as connection:
        connection.execute("DELETE FROM pulsara_v3.sessions WHERE id=%s", (sid,))
    with pytest.raises(KeyError):
        rename(repo, sid, "deleted")
    assert rows(repo, "SELECT id FROM pulsara_v3.sessions WHERE id=%s", (sid,)) == []


def test_controller_rename_uses_current_domain_without_loading_runtime():
    async def run():
        core = SimpleNamespace(rename_session=AsyncMock(return_value="saved"))
        controller = SimpleNamespace(_lock=asyncio.Lock(), _closing=False, core=core,
                                     workspace_input=SimpleNamespace(memory_domain_id="domain:test"))
        assert await LocalSessionController.rename_session(controller, "session:test", " saved ") == {
            "session_id": "session:test", "title": "saved"}
        core.rename_session.assert_awaited_once_with("session:test", memory_domain_id="domain:test", title=" saved ")
    asyncio.run(run())


def test_title_http_shape_security_and_missing_target(tmp_path):
    from typing import cast
    from aiohttp import ClientSession
    from pulsara_agent.web_app.http_server import LocalHttpServer
    from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
    from tests.test_local_web_http_surface import _Sessions, _Bridge, _model_server_dependencies

    async def run():
        sessions = _Sessions()
        sessions.rename_session = AsyncMock(return_value={"session_id": "one", "title": "new"})
        (tmp_path / "index.html").write_text("Pulsara")
        server = LocalHttpServer(sessions=cast(LocalSessionController, sessions),
            bridge=cast(LocalBrowserBridge, _Bridge()), static_root=tmp_path, requested_port=0,
            is_ready=lambda: True, is_draining=lambda: False, **_model_server_dependencies())
        await server.start()
        try:
            async with ClientSession() as client:
                url = f"{server.origin}/api/sessions/one/title"
                async with client.put(url, json={"title": "new"}, headers={"Origin": "https://other.test"}) as response:
                    assert response.status == 403
                for body in [{}, {"title": 3}, {"title": "new", "other": 1}]:
                    async with client.put(url, json=body) as response:
                        assert response.status == 400
                sessions.rename_session.assert_not_awaited()
                async with client.put(url, json={"title": "new"}) as response:
                    assert response.status == 200
                    assert await response.json() == {"session_id": "one", "title": "new"}
                sessions.rename_session.assert_awaited_once_with("one", "new")
                for error, status in [(KeyError("one"), 404), (ValueError("invalid"), 400)]:
                    sessions.rename_session.side_effect = error
                    async with client.put(url, json={"title": "new"}) as response:
                        assert response.status == status
        finally:
            await server.aclose()
    asyncio.run(run())
