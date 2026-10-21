"""Real pg_search snapshots and both canonical reads retain each candidate cut."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg import IsolationLevel
from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.history_excerpt import HistoryCandidate, earliest_hit
from pulsara_agent.conversation_kernel.history_projection import build_history_document
from pulsara_agent.conversation_kernel.history_publication import HistoryProjectionRepository
from pulsara_agent.conversation_kernel.history_search import (
    FrozenHistoryReadScope, PostgresHistoryQuery, search_history, refetch_history,
)
from pulsara_agent.ports.session_content import SessionContentRange
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_conversation_fork import identity, repo as repo, rows, turn
from tests.test_history_recall import new
from tests.test_history_publication import _ObservedProvider, _append, _q1


def deadline():
    return monotonic() + 30


def seed(repo, *, through=100):
    domain = uuid4().hex
    target = new(repo, domain)
    caller = new(repo, domain, rows(repo, 'SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s',(target.guard.session_id,))[0]['workspace_id'])
    owner, _, _ = turn(repo,target.guard,'中文起始🪐 rootboundary',finish=False)
    q1 = _q1(repo,target.guard.session_id,owner)
    entries = {}
    for sequence in range(2,through+1):
        text = f'进展🛰️ {sequence:04d} cutneedle{sequence:04d}。 ' + '真实中文 café Ω 😀 UTF8 paragraph. ' * 12
        entries[sequence] = _append(repo,target.guard,owner,text)
    scope = FrozenHistoryReadScope(caller.guard.session_id,domain,SessionContentRange(0))
    store = HistoryProjectionRepository(repo.connection_provider)
    assert store.maintain_question(target.guard.session_id,q1,deadline_monotonic=deadline()) == 'published'
    return target,caller,owner,q1,entries,scope,store


class ObservedReads:
    def __init__(self, provider):
        self.provider = provider
        self.reads = []

    @contextmanager
    def connection(self, **kwargs):
        with self.provider.connection(**kwargs) as connection:
            self.reads.append((connection,kwargs,connection.info.backend_pid))
            yield connection


def test_both_production_reads_keep_own_cut_after_replacement_without_retained_rr(
    repo, stage2_migrated_postgres_database,
):
    target,caller,owner,q1,entries,scope,store = seed(repo)
    sid = target.guard.session_id
    old = store.build_question(sid,q1,deadline_monotonic=deadline())
    for sequence in range(101,111):
        _append(repo,target.guard,owner,f'新进展 {sequence} future110needle')
    next_build = store.build_question(sid,q1,deadline_monotonic=deadline())
    for sequence in range(111,121):
        _append(repo,target.guard,owner,f'新进展 {sequence} future120needle',complete=sequence==120)
    last_build = store.build_question(sid,q1,deadline_monotonic=deadline())
    observed = ObservedReads(repo.connection_provider)
    query = PostgresHistoryQuery(observed)
    # Index is still C100 while canonical is already C120.
    candidates = query.automatic_candidates(scope=scope,query='cutneedle0100',deadline_monotonic=deadline())
    assert len(candidates)==1
    candidate = candidates[0]
    assert isinstance(candidate,HistoryCandidate)
    assert candidate.covered_entry_sequence==100 and candidate.variant=='QA' and candidate.include_tools is False
    assert candidate.document==old.documents[('QA',False)]
    assert candidate.document!=last_build.documents[('QA',False)]
    assert '[truncated]' in '\n'.join(m['text'] for m in candidate.messages)
    assert 'cutneedle0100' in '\n'.join(m['text'] for m in candidate.messages)
    assert set(candidate.projection())=={'source','title','messages','historical_advisory'}
    assert all('covered_entry_sequence' not in m for m in candidate.projection()['messages'])
    first_connection,first_options,first_pid = observed.reads[0]
    assert first_options['isolation_level']==IsolationLevel.REPEATABLE_READ
    assert first_connection.closed
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn,autocommit=True) as observer:
        assert observer.execute('SELECT pid FROM pg_stat_activity WHERE pid=%s',(first_pid,)).fetchall()==[]
    # An Event models the wait only; no remote rerank service is invoked.
    waiting,release = Event(),Event()
    def after_remote_wait():
        waiting.set()
        assert release.wait(20)
        return query.canonical_refetch(scope=scope,candidates=candidates,deadline_monotonic=deadline())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(after_remote_wait)
        try:
            assert waiting.wait(10)
            assert store.publish(next_build,deadline_monotonic=deadline())=='published'
            assert store.publish(last_build,deadline_monotonic=deadline())=='published'
            assert {r['covered_entry_sequence'] for r in rows(repo,'SELECT covered_entry_sequence FROM pulsara_v3.history_search_documents WHERE session_id=%s',(sid,))}=={120}
            assert all(r['body']!=candidate.document['body'] for r in rows(repo,'SELECT body FROM pulsara_v3.history_search_documents WHERE session_id=%s',(sid,)))
        finally:
            release.set()
        final = future.result(20)
    assert final==(candidate,) and final[0] is candidate
    assert len(observed.reads)==2
    second_connection,second_options,_ = observed.reads[-1]
    assert second_options['isolation_level']==IsolationLevel.REPEATABLE_READ
    assert second_connection.closed and second_connection is not first_connection
    # A later original Q2 changes the current group, never this candidate's cut.
    turn(repo,target.guard,'下一题🌏 q2boundary',finish=False)
    assert query.canonical_refetch(scope=scope,candidates=candidates,deadline_monotonic=deadline())==(candidate,)
    with repo.connection_provider.connection(lane=PostgresConnectionLane.MEMORY_QUERY,row_factory=dict_row,deadline_monotonic=deadline()) as c:
        live=build_history_document(c,sid,q1,covered_entry_sequence=121,variant='QAQ')
    assert live['messages'][-1]['region']=='Q2'
    assert candidate.document['q2_entry_id'] is None and all(m['region']!='Q2' for m in candidate.document['messages'])


@pytest.mark.parametrize('repeatable',[False,True])
def test_candidate_positions_interleaving_uses_real_engine_snapshot(repo,repeatable):
    target,caller,owner,q1,entries,scope,store = seed(repo)
    sid = target.guard.session_id
    old_body = rows(repo,'SELECT body FROM pulsara_v3.history_search_documents WHERE session_id=%s AND include_tools=false',(sid,))[0]['body']
    _append(repo,target.guard,owner,'新追加不应使原命中失去资格 futureboundary',complete=True)
    latest=store.build_question(sid,q1,deadline_monotonic=deadline())
    reached,release=Event(),Event()
    raw_positions=[]
    def before(sql,connection,_params):
        if 'paradedb.snippet_positions' in sql:
            reached.set()
            assert release.wait(20)
    def after(sql,connection,_params):
        if 'paradedb.snippet_positions' in sql:
            # A separate same-snapshot SELECT observes the body's actual version.
            raw_positions.append(connection.execute('SELECT body FROM pulsara_v3.history_search_documents WHERE session_id=%s AND include_tools=false',(sid,)).fetchone()['body'])
    provider=_ObservedProvider(repo.connection_provider,before=before,after=after)
    def retrieve():
        if repeatable:
            return PostgresHistoryQuery(provider).automatic_candidates(scope=scope,query='cutneedle0100',deadline_monotonic=deadline())
        with provider.connection(lane=PostgresConnectionLane.MEMORY_QUERY,row_factory=dict_row,deadline_monotonic=deadline()) as c:
            found=search_history(c,scope=scope,query='cutneedle0100',limit=20,automatic=True)
            assert len(found)==1 and found[0]['body']==old_body
            assert all(not found[0][field] for field in ('hits','hits_en','hits_tech'))
            return tuple(v for row in found if (v:=refetch_history(c,row,scope=scope,automatic=True)) is not None)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(retrieve)
        try:
            assert reached.wait(10)
            assert store.publish(latest,deadline_monotonic=deadline())=='published'
        finally:
            release.set()
        found=future.result(20)
    if repeatable:
        assert len(found)==1 and found[0].covered_entry_sequence==100 and found[0].document['body']==old_body
        assert raw_positions==[old_body]
        # Check original UTF-8 field mapping using actual engine hits.
        raw=old_body.encode()
        start=raw.index(b'cutneedle0100')
        assert raw[start:start+13].decode()=='cutneedle0100'
        assert earliest_hit(found[0].document,([[start,start+13]],))==(99,10)
    else:
        assert found==() and raw_positions!=[old_body]


def test_second_read_rechecks_current_scope_copy_visibility_corruption_and_delete(
    repo,stage2_migrated_postgres_database,
):
    target,caller,owner,q1,entries,scope,store=seed(repo,through=2)
    # Finish the ROOT with a new final entry so the fork uses a settled anchor.
    anchor=_append(repo,target.guard,owner,'完整最终回答',complete=True)
    assert store.maintain_question(target.guard.session_id,q1,deadline_monotonic=deadline())=='published'
    query=PostgresHistoryQuery(repo.connection_provider)
    candidates=query.automatic_candidates(scope=scope,query='cutneedle0002',deadline_monotonic=deadline())
    assert len(candidates)==1
    sid=target.guard.session_id
    self_scope=FrozenHistoryReadScope(sid,scope.memory_domain_id,SessionContentRange(3))
    assert query.canonical_refetch(scope=self_scope,candidates=candidates,deadline_monotonic=deadline())==candidates
    cases=[
        replace(scope,memory_domain_id='unauthorized'),
        replace(scope,session_id=identity('session')),
        replace(self_scope,current_range=SessionContentRange(2)),
        replace(self_scope,current_range=SessionContentRange(3,entries[2])),
        replace(scope,visible_entry_ids=(entries[2],)),
    ]
    other_project=new(repo,scope.memory_domain_id)
    cases.append(replace(scope,session_id=other_project.guard.session_id))
    other_domain=new(repo,uuid4().hex)
    cases.append(FrozenHistoryReadScope(other_domain.guard.session_id,rows(repo,'SELECT memory_domain_id FROM pulsara_v3.sessions WHERE id=%s',(other_domain.guard.session_id,))[0]['memory_domain_id'],SessionContentRange(0)))
    copied=repo.fork_conversation(source_session_id=sid,anchor_entry_id=anchor,child_session_id=identity('session'),memory_domain_id=scope.memory_domain_id,deadline_monotonic=deadline())
    assert copied.created
    copy_id=rows(repo,'SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND origin_entry_id=%s',(copied.child_session_id,entries[2]))[0]['id']
    assert copy_id!=entries[2]
    cases.append(FrozenHistoryReadScope(copied.child_session_id,scope.memory_domain_id,SessionContentRange(3),visible_entry_ids=(copy_id,)))
    for changed in cases:
        assert query.canonical_refetch(scope=changed,candidates=candidates,deadline_monotonic=deadline())==()
    # Actual canonical byte corruption must not be hidden by the old projection.
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn,row_factory=dict_row) as admin:
        saved=admin.execute('SELECT id,inline_content FROM pulsara_v3.assistant_message_blocks WHERE assistant_entry_id=%s',(entries[2],)).fetchone()
        admin.execute('UPDATE pulsara_v3.assistant_message_blocks SET inline_content=%s WHERE id=%s',(b'corrupt',saved['id']))
    try:
        assert query.canonical_refetch(scope=scope,candidates=candidates,deadline_monotonic=deadline())==()
    finally:
        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as admin:
            admin.execute('UPDATE pulsara_v3.assistant_message_blocks SET inline_content=%s WHERE id=%s',(saved['inline_content'],saved['id']))
    repo.release_host_writer(target.guard,deadline_monotonic=deadline())
    assert repo.delete_session(session_id=sid,memory_domain_id=scope.memory_domain_id,closed_writer=target.guard,required_lifecycle=None,deadline_monotonic=deadline())=='DELETED'
    assert query.canonical_refetch(scope=scope,candidates=candidates,deadline_monotonic=deadline())==()


def test_second_read_preserves_cut_q2_branch_and_rechecks_its_current_scope(repo):
    target,caller,owner,q1,entries,scope,store=seed(repo,through=2)
    _append(repo,target.guard,owner,'完整回答',complete=True)
    next_owner,_,_=turn(repo,target.guard,'后继问题 q2onlyboundary',finish=False)
    q2=_q1(repo,target.guard.session_id,next_owner)
    assert store.maintain_question(target.guard.session_id,q1,deadline_monotonic=deadline())=='published'
    query=PostgresHistoryQuery(repo.connection_provider)
    before_q2=FrozenHistoryReadScope(target.guard.session_id,scope.memory_domain_id,SessionContentRange(3))
    qa=query.automatic_candidates(scope=before_q2,query='cutneedle0002',deadline_monotonic=deadline())
    assert len(qa)==1 and qa[0].variant=='QA' and qa[0].document['q2_entry_id']==q2
    assert all(m['region']!='Q2' for m in qa[0].messages)
    assert query.canonical_refetch(scope=before_q2,candidates=qa,deadline_monotonic=deadline())==qa
    through_q2=replace(before_q2,current_range=SessionContentRange(4))
    # A QA cannot remain admitted when this scope instead selects its QAQ branch.
    assert query.canonical_refetch(scope=through_q2,candidates=qa,deadline_monotonic=deadline())==()
    qaq=query.automatic_candidates(scope=through_q2,query='cutneedle0002',deadline_monotonic=deadline())
    assert len(qaq)==1 and qaq[0].variant=='QAQ'
    visible_q2=replace(through_q2,visible_entry_ids=(q2,))
    assert query.canonical_refetch(scope=visible_q2,candidates=qaq,deadline_monotonic=deadline())==()
    visible_qa=query.automatic_candidates(scope=visible_q2,query='cutneedle0002',deadline_monotonic=deadline())
    assert len(visible_qa)==1 and visible_qa[0].variant=='QA'
    assert query.canonical_refetch(scope=visible_q2,candidates=visible_qa,deadline_monotonic=deadline())==visible_qa
    assert query.automatic_candidates(scope=visible_q2,query='q2onlyboundary',deadline_monotonic=deadline())==()
