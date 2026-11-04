from pathlib import Path

from pulsara_agent.conversation_kernel.current_work_runtime import CurrentWorkStateOwner, build_root_activation
from pulsara_agent.identity import derive_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.tools.builtins.todo import parse_todo_replacement


def activate(owner, turn):
    return owner.activate_root_run(build_root_activation(
        session_id=derive_id('session', 'test', 'media'), admission_kind='DIRECT',
        command_id=derive_id('command', 'test', turn), exact_turn_id=turn,
        exact_initial_entry_id=derive_id('entry', 'test', turn),
        exact_context_binding_revision_id=derive_id('binding', 'test', turn)))


def owner_and_turn():
    owner = CurrentWorkStateOwner(session_id=derive_id('session', 'test', 'media'),
                                  owner_epoch=derive_id('host', 'test', 'media'))
    turn = derive_id('turn', 'test', 'first')
    activate(owner, turn)
    return owner, turn


def read(owner, turn, path: Path, attempt: str, start=0, end=10):
    owner.reserve_media_read_start(exact_turn_id=turn,
        attempt_id=derive_id('attempt', 'test', attempt), canonical_path=str(path),
        file_mtime_ns=str(path.stat().st_mtime_ns))
    return owner.observe_media_read_start(exact_turn_id=turn,
        attempt_id=derive_id('attempt', 'test', attempt), canonical_path=str(path),
        file_mtime_ns=str(path.stat().st_mtime_ns), from_seconds=start, to_seconds=end)


def test_media_ranges_keep_latest_actual_start_through_continuation_and_idle(tmp_path):
    owner, turn = owner_and_turn()
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'video')
    assert owner.current_work_snapshot().media_ranges == ()
    assert read(owner, turn, media, 'A', 0, 10)
    assert read(owner, turn, media, 'B', 20, 30)
    assert not owner.observe_media_read_start(exact_turn_id=turn, attempt_id=derive_id('attempt', 'test', 'A'), canonical_path=str(media), file_mtime_ns=str(media.stat().st_mtime_ns), from_seconds=1, to_seconds=9)
    retained = owner.current_work_snapshot().media_ranges
    assert len(retained) == 1 and retained[0].from_seconds == 20
    # A different tool/failed read has no update path. Continuation preserves it.
    next_turn = derive_id('turn', 'test', 'continued')
    owner.bind_continuation(scope_kind=ModelInputScopeKind.ROOT, turn_id=next_turn)
    assert owner.current_work_snapshot().media_ranges == retained
    assert not read(owner, turn, media, 'late-old-turn')
    assert owner.mark_root_idle(exact_turn_id=turn) is None
    assert owner.current_work_snapshot().media_ranges == retained
    installation = owner.mark_root_idle(exact_turn_id=next_turn)
    assert installation is not None and not installation.todos[0].ordered_items
    assert owner.current_work_snapshot().media_ranges == ()
    assert not read(owner, next_turn, media, 'idle-callback')
    # An old callback cannot update the successor ROOT work.
    successor = derive_id('turn', 'test', 'successor')
    activate(owner, successor)
    assert not read(owner, next_turn, media, 'old-work')
    assert read(owner, successor, media, 'new-work')
    owner.close_run(scope_kind=ModelInputScopeKind.ROOT)
    assert owner.current_work_snapshot().media_ranges == ()


def test_todo_commit_and_media_ranges_are_independent_values_in_one_owner(tmp_path):
    owner, turn = owner_and_turn()
    media = tmp_path / 'speech.wav'
    media.write_bytes(b'audio')
    assert read(owner, turn, media, 'read')
    retained = owner.current_work_snapshot().media_ranges
    prepared = owner.prepare_replace(scope_kind=ModelInputScopeKind.ROOT,
        exact_turn_id=turn, attempt_id=derive_id('attempt', 'test', 'todo'),
        proposed_result_entry_id=derive_id('entry', 'test', 'todo'),
        candidate=parse_todo_replacement({'items': [{'text': 'Review evidence', 'status': 'pending'}]}),
        acknowledgement=b'ok')
    assert owner.current_work_snapshot().todos[0].ordered_items == ()
    owner.commit(prepared)
    snapshot = owner.current_work_snapshot()
    assert snapshot.media_ranges == retained
    assert snapshot.todos[0].ordered_items[0].text == 'Review evidence'
    owner.mark_root_idle(exact_turn_id=turn)
    snapshot = owner.current_work_snapshot()
    assert snapshot.media_ranges == () and snapshot.todos[0].ordered_items == ()


def test_source_invalidation_removes_only_affected_media(tmp_path):
    owner, turn = owner_and_turn()
    one = tmp_path / 'one.mp4'
    one.write_bytes(b'one')
    two = tmp_path / 'two.wav'
    two.write_bytes(b'two')
    read(owner, turn, one, 'one')
    read(owner, turn, two, 'two')
    one.unlink()
    assert [item.canonical_path for item in owner.current_work_snapshot().media_ranges] == [str(two)]
    two.write_bytes(b'changed')
    assert owner.current_work_snapshot().media_ranges == ()


def test_reserved_new_read_rejects_older_scan_and_previous_source_version(tmp_path):
    owner, turn = owner_and_turn()
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'old')
    old_mtime = str(media.stat().st_mtime_ns)
    older = derive_id('attempt', 'test', 'old-scan')
    newer = derive_id('attempt', 'test', 'new-scan')
    owner.reserve_media_read_start(exact_turn_id=turn, attempt_id=older,
        canonical_path=str(media), file_mtime_ns=old_mtime)
    media.write_bytes(b'new source')
    new_mtime = str(media.stat().st_mtime_ns)
    owner.reserve_media_read_start(exact_turn_id=turn, attempt_id=newer,
        canonical_path=str(media), file_mtime_ns=new_mtime)
    assert not owner.observe_media_read_start(exact_turn_id=turn, attempt_id=older,
        canonical_path=str(media), file_mtime_ns=old_mtime, from_seconds=0, to_seconds=10)
    assert owner.observe_media_read_start(exact_turn_id=turn, attempt_id=newer,
        canonical_path=str(media), file_mtime_ns=new_mtime, from_seconds=20, to_seconds=30)
    retained = owner.current_work_snapshot().media_ranges
    assert not owner.observe_media_read_start(exact_turn_id=turn, attempt_id=older,
        canonical_path=str(media), file_mtime_ns=old_mtime, from_seconds=1, to_seconds=9)
    assert owner.current_work_snapshot().media_ranges == retained
