"""History search contracts against the real pg_search engine and canonical owners."""

from time import monotonic
import pytest
from pulsara_agent.conversation_kernel.history_search import (
    FrozenHistoryReadScope,
    search_history,
    refetch_history,
)
from pulsara_agent.conversation_kernel.history_projection import (
    rebuild_history_projection,
)
from pulsara_agent.conversation_kernel.history_excerpt import (
    excerpt_history,
    earliest_hit,
    ESTIMATOR,
)
from pulsara_agent.conversation_kernel.session_content import SessionContentQuery
from pulsara_agent.ports.session_content import SessionContentRange
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from psycopg.rows import dict_row
from tests.test_conversation_fork import repo as repo, identity, turn, rows, fork


def new(repo, domain, workspace=None):
    return repo.acquire_host_writer(
        intent="NEW",
        session_id=identity("session"),
        workspace_id=workspace or identity("workspace"),
        memory_domain_id=domain,
        writer_owner_id=identity("host"),
        lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    )


def search(repo, scope, query, **kwargs):
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_QUERY,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as c:
        found = search_history(c, scope=scope, query=query, limit=20, **kwargs)
        return found, tuple(
            value
            for r in found
            if (
                value := refetch_history(
                    c, r, scope=scope, include_tools=kwargs.get("include_tools", False)
                )
            )
            is not None
        )


@pytest.mark.parametrize(
    "query", ["检索", "runs", "API_key", "qwen3.5", "/src/my-file.py"]
)
def test_engine_actual_fields_byte_positions_and_shared_tool_results(repo, query):
    domain = identity("domain").replace(":", "_")
    caller = new(repo, domain)
    target = new(repo, domain)
    turn(
        repo,
        target.guard,
        "研究中文检索",
        answer="中文检索 qwen3.5 API_key /src/my-file.py running shoes 😀",
    )
    scope = FrozenHistoryReadScope(
        caller.guard.session_id, domain, SessionContentRange(0)
    )
    found, candidates = search(
        repo, scope, query, target_session_id=target.guard.session_id
    )
    assert len(candidates) == 1
    assert (
        earliest_hit(
            candidates[0].document,
            (found[0]["hits"], found[0]["hits_en"], found[0]["hits_tech"]),
        )
        is not None
    )
    tool = SessionContentQuery(
        repo, session_id=caller.guard.session_id, memory_domain_id=domain
    ).invoke(
        "search_session_content",
        {"session_id": target.guard.session_id, "query": query},
        current_range=scope.current_range,
        tool_call_id="call:history",
        deadline_monotonic=monotonic() + 30,
    )
    assert tool["items"][0]["messages"] == [
        {k: m[k] for k in ("entry_id", "role", "label", "at", "text")}
        for m in candidates[0].messages
    ]
    if query == "runs":
        assert found[0]["hits"] is None and found[0]["hits_tech"] is None
        assert found[0]["hits_en"]


def test_QA_QAQ_mutually_exclusive_eligibility_before_topk(repo):
    domain = identity("domain").replace(":", "_")
    caller = new(repo, domain)
    sid = caller.guard.session_id
    _, _, a = turn(repo, caller.guard, "first alpha", answer="answer beta")
    _, _, _ = turn(repo, caller.guard, "next exclusive-Q2", answer="second answer")
    bound = rows(
        repo,
        "SELECT entry_sequence FROM pulsara_v3.transcript_entries WHERE id=%s",
        (a,),
    )[0]["entry_sequence"]
    scope = FrozenHistoryReadScope(sid, domain, SessionContentRange(bound))
    found, candidates = search(repo, scope, "beta")
    assert len(candidates) == 1 and found[0]["variant"] == "QA"
    assert all(m["region"] != "Q2" for m in candidates[0].messages)
    assert search(repo, scope, "exclusive-Q2")[0] == []
    partial = FrozenHistoryReadScope(sid, domain, SessionContentRange(bound - 1))
    assert search(repo, partial, "alpha")[0] == []


def test_fork_exact_copy_folded_before_limit_parent_delete_and_rebuild(repo):
    domain = "u_local"
    source = new(repo, domain)
    _, _, anchor = turn(
        repo, source.guard, "fork-original-keyword", answer="saved original answer"
    )
    child = fork(repo, source.guard.session_id, anchor)
    assert child.created
    caller = new(
        repo,
        domain,
        rows(
            repo,
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (source.guard.session_id,),
        )[0]["workspace_id"],
    )
    scope = FrozenHistoryReadScope(
        caller.guard.session_id, domain, SessionContentRange(0)
    )
    found, candidates = search(repo, scope, "fork-original-keyword", automatic=True)
    assert len(candidates) == 1
    originals = rows(
        repo,
        "SELECT id,origin_entry_id FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
        (source.guard.session_id,),
    )
    copies = rows(
        repo,
        "SELECT id,origin_entry_id FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
        (child.child_session_id,),
    )
    assert [x["origin_entry_id"] for x in copies] == [x["id"] for x in originals]
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_QUERY,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as c:
        rebuild_history_projection(c, page_size=1)
    assert len(search(repo, scope, "fork-original-keyword", automatic=True)[1]) == 1
    assert (
        repo.delete_session(
            session_id=source.guard.session_id,
            memory_domain_id=domain,
            closed_writer=source.guard,
            deadline_monotonic=monotonic() + 30,
        )
        == "DELETED"
    )
    candidates = search(repo, scope, "fork-original-keyword", automatic=True)[1]
    assert len(candidates) == 1 and candidates[0].session_id == child.child_session_id


def test_project_scope_and_visible_precise_fork_exclusion(repo):
    domain = "u_local"
    source = new(repo, domain)
    other = new(repo, domain)
    _, _, anchor = turn(
        repo, source.guard, "scope-only-needle", answer="relevant answer"
    )
    turn(repo, other.guard, "scope-only-needle " * 30, answer="wrong project")
    copy = fork(repo, source.guard.session_id, anchor)
    assert copy.created
    visible_ids = tuple(
        x["id"]
        for x in rows(
            repo,
            "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s",
            (source.guard.session_id,),
        )
    )
    scope = FrozenHistoryReadScope(
        source.guard.session_id, domain, SessionContentRange(0), visible_ids
    )
    assert search(repo, scope, "scope-only-needle", automatic=True)[0] == []
    caller = new(
        repo,
        domain,
        rows(
            repo,
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (source.guard.session_id,),
        )[0]["workspace_id"],
    )
    scope = FrozenHistoryReadScope(
        caller.guard.session_id, domain, SessionContentRange(0)
    )
    found, candidates = search(repo, scope, "scope-only-needle", automatic=True)
    assert len(candidates) == 1 and all(
        x.session_id != other.guard.session_id for x in candidates
    )


def test_relevance_cursor_lossless_and_old_cursor_rejected(repo, stage2_migrated_postgres_database):
    domain = identity("domain").replace(":", "_")
    caller = new(repo, domain)
    target = new(repo, domain)
    for i in range(7):
        turn(
            repo,
            target.guard,
            f"page-keyword question {i}",
            answer=f"page-keyword answer {i}",
        )
    # This test promises fixed scoring statistics. Prior tests and these turns
    # leave deleted projection versions; VACUUM/merge can otherwise change BM25
    # scores mid-page without any new logical writes. Rebuild this test index
    # from live rows before checking exact keyset continuity.
    import psycopg
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn, autocommit=True) as connection:
        connection.execute("REINDEX INDEX pulsara_v3.idx_pulsara_v3_history_bm25")
    owner = SessionContentQuery(
        repo, session_id=caller.guard.session_id, memory_domain_id=domain
    )

    def invoke(args):
        return owner.invoke(
            "search_session_content",
            args,
            current_range=SessionContentRange(0),
            tool_call_id="call:history",
            deadline_monotonic=monotonic() + 30,
        )

    all_items = invoke(
        {"session_id": target.guard.session_id, "query": "page-keyword"}
    )["items"]
    page = invoke(
        {"session_id": target.guard.session_id, "query": "page-keyword", "limit": 2}
    )
    items = list(page["items"])
    while page["next_cursor"]:
        page = invoke({"cursor": page["next_cursor"], "limit": 2})
        items.extend(page["items"])
    assert [(x["entry_id"], x["score"]) for x in items] == [
        (x["entry_id"], x["score"]) for x in all_items
    ]
    assert len({x["entry_id"] for x in items}) == 7


def doc(texts):
    messages = []
    positions = []
    body = []
    offset = 0
    for i, (region, text, annotated) in enumerate(texts):
        messages.append(
            {
                "region": region,
                "entry_id": f"entry:{i}",
                "text": text,
                "annotated": annotated,
                "sequence": i,
                "role": "user" if region != "A" else "assistant",
                "at": "now",
                "label": region,
            }
        )
        if body:
            offset += 1
        size = len(text.encode())
        positions.append(
            {"start": offset, "end": offset + size, "message": i, "display_start": 0}
        )
        body.append(text)
        offset += size
    return {"messages": messages, "positions": positions, "body": "\n".join(body)}


def test_excerpt_earliest_unicode_hit_soft_quota_return_and_determinism():
    d = doc(
        [
            ("Q1", "问" * 100, False),
            ("A", "答" * 1000 + "😀needle" + "后" * 1000, False),
            ("Q2", "追" * 100, False),
        ]
    )
    start = d["body"].encode().index(b"needle")
    hits = ([[start, start + 6]], None, None)
    result = excerpt_history(d, hits)
    assert result == excerpt_history(d, hits)
    assert (
        sum(
            ESTIMATOR.estimate_text(
                m["text"].replace("[truncated]\n", "").replace("\n[truncated]", "")
            )
            for m in result
        )
        <= 1024
    )
    answer = next(m for m in result if m["region"] == "A")
    assert "needle" in answer["text"] and "😀" in answer["text"]
    assert (
        sum(
            ESTIMATOR.estimate_text(
                m["text"].replace("[truncated]\n", "").replace("\n[truncated]", "")
            )
            for m in result
            if m["region"] == "A"
        )
        == 824
    )


def test_annotation_hit_is_atomic_or_candidate_excluded_and_no_later_center():
    d = doc(
        [
            ("Q1", "q", False),
            ("A", "x" * 5000 + " needle", True),
            ("Q2", "later needle", False),
        ]
    )
    start = d["body"].encode().index(b"needle")
    later = d["body"].encode().rindex(b"needle")
    assert (
        excerpt_history(d, ([[later, later + 6], [start, start + 6]], None, None))
        is None
    )
    d = doc(
        [("Q1", "q", False), ("A", "x" * 3000 + " needle", True), ("Q2", "tail", False)]
    )
    start = d["body"].encode().index(b"needle")
    result = excerpt_history(d, ([[start, start + 6]], None, None))
    assert any(m["text"] == d["messages"][1]["text"] for m in result)


def test_global_annotation_barriers_block_far_questions_and_second_annotation():
    d = doc(
        [
            ("Q1", "far upper", False),
            ("A", "注" * 1100, True),
            ("A", "near " * 40 + "needle" + " after " * 40, False),
            ("A", "small annotated", True),
            ("Q2", "far lower", False),
        ]
    )
    start = d["body"].encode().index(b"needle")
    result = excerpt_history(d, ([[start, start + 6]], None, None))
    assert result is not None
    assert all(
        "far upper" not in m["text"]
        and "far lower" not in m["text"]
        and "small annotated" not in m["text"]
        for m in result
    )
    assert any("needle" in m["text"] for m in result)


def test_annotation_labels_do_not_consume_exact_body_budget_and_no_soft_extra():
    d = doc(
        [
            ("Q1", "question", False),
            ("A", "PACKAGING:" + "注" * 1024, True),
            ("Q2", "later", False),
        ]
    )
    d["messages"][1]["body_ranges"] = [
        [len("PACKAGING:"), len(d["messages"][1]["text"])]
    ]
    start = d["body"].encode().index("注".encode())
    result = excerpt_history(d, ([[start, start + 3]], None, None))
    assert len(result) == 1
    assert result[0]["text"] == "[truncated]\n" + d["messages"][1]["text"] + "\n[truncated]"
    d["messages"][1]["body_ranges"] = [
        [len("PACKAGING:") - 1, len(d["messages"][1]["text"])]
    ]
    assert excerpt_history(d, ([[start, start + 3]], None, None)) is None


def test_visible_Q2_selects_QA_and_its_words_never_enter_score(repo):
    domain = "u_local"
    source = new(repo, domain)
    _, _, anchor = turn(
        repo, source.guard, "first question", answer="visible-q2-fallback answer"
    )
    second, _, _ = turn(
        repo, source.guard, "exclusiveq2identifier", answer="second answer"
    )
    q2 = rows(
        repo, "SELECT initial_entry_id FROM pulsara_v3.turns WHERE id=%s", (second,)
    )[0]["initial_entry_id"]
    # The exact installed visible entry may also exclude its copy in another fork.
    scope = FrozenHistoryReadScope(
        source.guard.session_id,
        domain,
        SessionContentRange(1000),
        (q2,),
    )
    found, candidates = search(repo, scope, "visible-q2-fallback", automatic=True)
    assert len(candidates) == 1 and found[0]["variant"] == "QA"
    assert all(m["region"] != "Q2" for m in candidates[0].messages)
    assert search(repo, scope, "exclusiveq2identifier", automatic=True)[0] == []


def test_automatic_invalid_excerpts_do_not_fetch_the_twenty_first_unit(repo, monkeypatch):
    from pulsara_agent.conversation_kernel import history_search

    domain = identity('domain').replace(':', '_')
    caller = new(repo, domain)
    workspace = rows(repo, 'SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s', (caller.guard.session_id,))[0]['workspace_id']
    target = new(repo, domain, workspace)
    for i in range(21):
        turn(repo, target.guard, f'budgetprobe question {i}', answer=f'budgetprobe answer {i}')
    scope = FrozenHistoryReadScope(caller.guard.session_id, domain, SessionContentRange(0))
    with repo.connection_provider.connection(lane=PostgresConnectionLane.MEMORY_QUERY, row_factory=dict_row, deadline_monotonic=monotonic()+30) as connection:
        assert len(search_history(connection, scope=scope, query='budgetprobe', limit=21, automatic=True)) == 21
    admissions = []
    original = history_search.search_history
    def observe(connection, **kwargs):
        admitted = original(connection, **kwargs)
        admissions.append((kwargs['limit'], len(admitted)))
        return admitted
    monkeypatch.setattr(history_search, 'search_history', observe)
    monkeypatch.setattr(history_search, 'excerpt_history', lambda *_: None)
    result = history_search.PostgresHistoryQuery(repo.connection_provider).automatic_candidates(scope=scope, query='budgetprobe', deadline_monotonic=monotonic()+30)
    assert result == ()
    assert admissions == [(20, 20)]


def test_unrestorable_Q2_selects_complete_QA_without_its_query_terms(repo, monkeypatch):
    from pulsara_agent.conversation_kernel import history_projection
    domain = identity('domain').replace(':', '_')
    caller = new(repo, domain)
    target = new(repo, domain)
    turn(repo, target.guard, 'restorablequestion', answer='restorableanswer')
    turn(repo, target.guard, 'unrestorableq2sentinel', answer='later answer')
    q2 = rows(repo, "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_kind='USER_MESSAGE' ORDER BY entry_sequence DESC LIMIT 1", (target.guard.session_id,))[0]['id']
    original = history_projection.canonical_history_message
    def unavailable(connection, entry, **kwargs):
        if entry['id'] == q2:
            raise RuntimeError('canonical Q2 restoration unavailable')
        return original(connection, entry, **kwargs)
    monkeypatch.setattr(history_projection, 'canonical_history_message', unavailable)
    with repo.connection_provider.connection(lane=PostgresConnectionLane.MEMORY_QUERY, row_factory=dict_row, deadline_monotonic=monotonic()+30) as connection:
        rebuild_history_projection(connection)
    scope = FrozenHistoryReadScope(caller.guard.session_id, domain, SessionContentRange(0))
    found, candidates = search(repo, scope, 'restorableanswer', target_session_id=target.guard.session_id)
    assert len(candidates) == 1 and found[0]['variant'] == 'QA'
    assert all(m['region'] != 'Q2' for m in candidates[0].messages)
    assert search(repo, scope, 'unrestorableq2sentinel', target_session_id=target.guard.session_id)[0] == []


def test_explicit_pagination_continues_after_a_full_page_of_invalid_excerpts(repo, monkeypatch):
    from pulsara_agent.conversation_kernel import history_search
    domain = identity('domain').replace(':', '_')
    caller, target = new(repo, domain), new(repo, domain)
    for i in range(4):
        turn(repo, target.guard, f'continueprobe question {i}', answer='continueprobe answer')
    owner = SessionContentQuery(repo, session_id=caller.guard.session_id, memory_domain_id=domain)
    original = history_search.refetch_history
    skipped = []
    def reject_first_page(connection, row, **kwargs):
        if len(skipped) < 2:
            skipped.append(row['q1_entry_id'])
            return None
        return original(connection, row, **kwargs)
    monkeypatch.setattr(history_search, 'refetch_history', reject_first_page)
    def invoke(arguments):
        return owner.invoke('search_session_content', arguments, current_range=SessionContentRange(0), tool_call_id='call:pagination', deadline_monotonic=monotonic()+30)
    page = invoke({'session_id':target.guard.session_id,'query':'continueprobe','limit':1})
    assert page['items'] == [] and page['next_cursor'] is not None
    following = invoke({'cursor':page['next_cursor'],'limit':1})
    assert following['items'] and following['items'][0]['entry_id'] not in skipped


@pytest.mark.parametrize("quote_length", [20, 700])
def test_center_annotation_expands_both_sides_and_redistributes_budget(quote_length):
    d = doc([
        ("Q1", "问", False),
        ("A", "前" * 2000, False),
        ("A", "注" * quote_length + "needle", True),
        ("A", "后" * 2000, False),
        ("Q2", "追", False),
    ])
    start = d["body"].encode().index(b"needle")
    result = excerpt_history(d, ([[start, start + 6]], None, None))
    texts = [m["text"] for m in result]
    assert d["messages"][2]["text"] in texts
    assert any("前" in text for text in texts)
    assert any("后" in text for text in texts)
    body = "".join(text.replace("[truncated]", "").replace("\n", "") for text in texts)
    assert 1023 <= ESTIMATOR.estimate_text(body) <= 1024


@pytest.mark.parametrize("failure", ["postgres", "statement_timeout", "decode"])
def test_projection_failure_preserves_canonical_turn_and_later_refresh(repo, monkeypatch, failure):
    from pulsara_agent.conversation_kernel import history_projection
    domain = identity("domain").replace(":", "_")
    source = new(repo, domain)
    caller = new(repo, domain, rows(repo, "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s", (source.guard.session_id,))[0]["workspace_id"])
    original = history_projection.refresh_history_question

    def broken(connection, session_id, q1_id):
        # Exercise rollback of partial projection writes, not just a pre-write error.
        original(connection, session_id, q1_id)
        if failure == "postgres":
            connection.execute("SELECT 1 / 0")
        if failure == "statement_timeout":
            connection.execute("SET LOCAL statement_timeout = 1")
            connection.execute("SELECT pg_sleep(0.02)")
        raise ValueError("projection decode failure")

    with monkeypatch.context() as patch:
        patch.setattr(history_projection, "refresh_history_question", broken)
        _, _, final = turn(repo, source.guard, "projection-failure-needle", answer="canonical answer")
    assert rows(repo, "SELECT id FROM pulsara_v3.transcript_entries WHERE id=%s", (final,))
    assert rows(repo, "SELECT id FROM pulsara_v3.history_search_documents WHERE session_id=%s", (source.guard.session_id,)) == []
    # A later ordinary write can rebuild the affected predecessor, without jobs.
    turn(repo, source.guard, "next question", answer="next answer")
    scope = FrozenHistoryReadScope(caller.guard.session_id, domain, SessionContentRange(0))
    assert search(repo, scope, "projection-failure-needle", automatic=True)[1]


@pytest.mark.parametrize("failure", ["postgres", "statement_timeout", "decode"])
def test_projection_failure_does_not_prevent_fork(repo, monkeypatch, failure):
    from pulsara_agent.conversation_kernel import history_projection
    source = new(repo, "u_local")
    _, _, anchor = turn(repo, source.guard, "fork-projection-failure", answer="canonical child content")
    original = history_projection.refresh_history_question

    def broken(connection, session_id, q1_id):
        original(connection, session_id, q1_id)
        if failure == "postgres":
            connection.execute("SELECT 1 / 0")
        if failure == "statement_timeout":
            connection.execute("SET LOCAL statement_timeout = 1")
            connection.execute("SELECT pg_sleep(0.02)")
        raise ValueError("projection decode failure")

    with monkeypatch.context() as patch:
        patch.setattr(history_projection, "refresh_history_question", broken)
        child = fork(repo, source.guard.session_id, anchor)
    assert child.created, child.public_code
    assert len(rows(repo, "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s", (child.child_session_id,))) == 2
    assert rows(repo, "SELECT id FROM pulsara_v3.history_search_documents WHERE session_id=%s", (child.child_session_id,)) == []


def test_failed_projection_refresh_leaves_old_rows_but_never_returns_stale_body(repo, monkeypatch):
    from datetime import datetime, timezone
    from pulsara_agent.conversation_kernel import history_projection
    from pulsara_agent.conversation_kernel.contracts import InlineContent
    from pulsara_agent.conversation_kernel.repository import AssistantTextBlock

    domain = identity("domain").replace(":", "_")
    source = new(repo, domain)
    workspace = rows(repo, "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s", (source.guard.session_id,))[0]["workspace_id"]
    caller = new(repo, domain, workspace)
    turn_id, _, _ = turn(repo, source.guard, "stale-projection-needle", answer="old progress", complete=False)
    cut = repo.prepare_provider_input_cut(source.guard, turn_id=turn_id, deadline_monotonic=monotonic()+30)
    scope = FrozenHistoryReadScope(caller.guard.session_id, domain, SessionContentRange(0))
    assert search(repo, scope, "stale-projection-needle", automatic=True)[1]
    original = history_projection.refresh_history_question

    def broken(connection, session_id, q1_id):
        original(connection, session_id, q1_id)
        connection.execute("SELECT 1 / 0")

    with monkeypatch.context() as patch:
        patch.setattr(history_projection, "refresh_history_question", broken)
        repo.commit_assistant_message(
            source.guard, cut=cut, entry_id=identity("entry"),
            parent_content=InlineContent.from_bytes(b"manifest"),
            blocks=(AssistantTextBlock(identity("block"), InlineContent.from_bytes(b"new canonical answer")),),
            complete_turn=True, occurred_at=datetime.now(timezone.utc),
            actor_id="model:test", deadline_monotonic=monotonic()+30,
        )
    found, candidates = search(repo, scope, "stale-projection-needle", automatic=True)
    assert found  # Savepoint restored the old derived rows.
    assert candidates == ()  # Canonical comparison rejects their stale body.


def test_relevance_paging_survives_corpus_score_drift(repo):
    """Unrelated canonical writes must not strand results above an old score."""
    domain = identity('domain').replace(':', '_')
    caller, target = new(repo, domain), new(repo, domain)
    for i in range(2):
        turn(repo, target.guard, f'scoredriftneedle question {i}', answer='scoredriftneedle answer')
    owner = SessionContentQuery(repo, session_id=caller.guard.session_id, memory_domain_id=domain)

    def invoke(args):
        return owner.invoke('search_session_content', args, current_range=SessionContentRange(0), tool_call_id='call:drift', deadline_monotonic=monotonic()+30)

    args = {'session_id': target.guard.session_id, 'query': 'scoredriftneedle'}
    before = invoke(args)['items']
    first = invoke({**args, 'limit': 1})
    assert len(before) == 2 and first['next_cursor']
    unrelated = new(repo, identity('domain').replace(':', '_'))
    for i in range(20):
        turn(repo, unrelated.guard, f'unrelated corpus document {i}', answer='independent background material ' * 400)
    now = invoke(args)['items']
    assert [item['entry_id'] for item in now] == [item['entry_id'] for item in before]
    # This is the actual failure condition, not merely a mock score change:
    # the old score cursor would now exclude *both* unchanged source units.
    assert min(item['score'] for item in now) > first['items'][0]['score']
    second = invoke({'cursor': first['next_cursor'], 'limit': 1})
    assert [item['entry_id'] for item in first['items'] + second['items']] == [item['entry_id'] for item in before]
    assert second['next_cursor'] is None


def test_relevance_cursor_does_not_consume_budget_rejected_item(repo, monkeypatch):
    from pulsara_agent.conversation_kernel import session_content
    domain = identity('domain').replace(':', '_')
    caller, target = new(repo, domain), new(repo, domain)
    for i in range(3):
        turn(repo, target.guard, f'pagebudgetneedle question {i}', answer='pagebudgetneedle answer')
    owner = SessionContentQuery(repo, session_id=caller.guard.session_id, memory_domain_id=domain)

    def invoke(args):
        return owner.invoke('search_session_content', args, current_range=SessionContentRange(0), tool_call_id='call:budget', deadline_monotonic=monotonic()+30)

    args = {'session_id': target.guard.session_id, 'query': 'pagebudgetneedle'}
    expected = [i['entry_id'] for i in invoke(args)['items']]
    real_fits = session_content._fits
    monkeypatch.setattr(session_content, '_fits', lambda payload, call_id: len(payload.get('items', [])) <= 1 and real_fits(payload, call_id))
    page = invoke({**args, 'limit': 2})
    items = list(page['items'])
    while page['next_cursor']:
        page = invoke({'cursor': page['next_cursor'], 'limit': 2})
        items.extend(page['items'])
    assert [i['entry_id'] for i in items] == expected


def test_relevance_paging_reorders_unseen_results_and_replays_prior_cursor(repo, monkeypatch):
    from pulsara_agent.conversation_kernel import history_search
    from pulsara_agent.conversation_kernel.session_content import _decode, _encode, SessionQueryError
    domain = identity('domain').replace(':', '_')
    caller, target = new(repo, domain), new(repo, domain)
    for i in range(4):
        turn(repo, target.guard, f'reorderneedle question {i}', answer='reorderneedle answer')
    owner = SessionContentQuery(repo, session_id=caller.guard.session_id, memory_domain_id=domain)

    def invoke(args, query_owner=owner):
        return query_owner.invoke('search_session_content', args, current_range=SessionContentRange(0), tool_call_id='call:reorder', deadline_monotonic=monotonic()+30)

    args = {'session_id': target.guard.session_id, 'query': 'reorderneedle'}
    expected = [i['entry_id'] for i in invoke(args)['items']]
    first = invoke({**args, 'limit': 1})
    first_cursor = first['next_cursor']
    original = history_search.search_history
    # Force the relative order to change, independent of engine statistics.
    # The real SQL still owns qualification, consumed-ID exclusion and refetch.
    def changed_order(connection, **kwargs):
        limit = kwargs.pop('limit')
        found = original(connection, **kwargs, limit=20)
        return sorted(found, key=lambda row: expected.index(row['q1_entry_id']), reverse=True)[:limit]
    monkeypatch.setattr(history_search, 'search_history', changed_order)
    second = invoke({'cursor': first_cursor, 'limit': 1})
    assert second['items'][0]['entry_id'] == expected[-1]
    replay = invoke({'cursor': first_cursor, 'limit': 1})
    assert replay['items'] == second['items']
    items = first['items'] + second['items']
    page = second
    while page['next_cursor']:
        page = invoke({'cursor': page['next_cursor'], 'limit': 1})
        items.extend(page['items'])
    assert len(items) == len(expected)
    assert {i['entry_id'] for i in items} == set(expected)
    # No cross-restart recovery or cursor authority: restart explicitly, and
    # never permit a real continuation to be paired with changed query values.
    restarted = SessionContentQuery(repo, session_id=caller.guard.session_id, memory_domain_id=domain)
    with pytest.raises(SessionQueryError) as error:
        invoke({'cursor': first_cursor}, restarted)
    assert error.value.code == 'CURSOR_EXPIRED'
    token = _decode(first_cursor, 'search_session_content')
    for key, value in [('query', 'another'), ('session_id', caller.guard.session_id), ('include_tools', True)]:
        with pytest.raises(SessionQueryError) as error:
            invoke({'cursor': _encode(dict(token, **{key: value}))})
        assert error.value.code == 'CURSOR_INVALID'
    token['operation'] = 'search_session_content.bm25.v1'
    token['after'] = ['0x1.0p+0', target.guard.session_id, expected[0]]
    del token['consumed_page']
    with pytest.raises(SessionQueryError) as error:
        invoke({'cursor': _encode(token)})
    assert error.value.code == 'CURSOR_INVALID'


@pytest.mark.parametrize('query', ['的 了 和 是', 'the is and to', '的 the 了 IS and', '啊 呀 呢 吧 嘛 么 嗯', 'a an am was were being', '的啊 the BE', '呗 嘞 喽 噢 哟 唷 嘿 呵 哇', 'please PLEASE'])
def test_shared_lexical_analyzer_stopwords_do_not_match_any_field(repo, query):
    domain = identity('domain').replace(':', '_')
    caller = new(repo, domain)
    target = new(repo, domain)
    turn(repo, target.guard, '的 了 和 是 the is and to 啊 呀 呢 吧 嘛 么 嗯 a an am was were being BE 呗 嘞 喽 噢 哟 唷 嘿 呵 哇 please PLEASE', answer='没有相关实词')
    scope = FrozenHistoryReadScope(caller.guard.session_id, domain, SessionContentRange(0))
    assert search(repo, scope, query, target_session_id=target.guard.session_id)[0] == []


def test_stopword_filter_keeps_body_positions_and_removes_unrelated_matches(repo):
    domain = identity('domain').replace(':', '_')
    caller = new(repo, domain)
    target = new(repo, domain)
    turn(repo, target.guard, '的 了 the 哇 please', answer='无关旧任务')
    turn(repo, target.guard, '的 了 the 哇 please 紫色纸船', answer='中文检索 running API_key /src/my-file.py')
    scope = FrozenHistoryReadScope(caller.guard.session_id, domain, SessionContentRange(0))
    found, candidates = search(repo, scope, '的 了 the 哇 please API_key', target_session_id=target.guard.session_id)
    assert len(candidates) == 1
    raw = found[0]['body'].encode()
    terms = {
        raw[start:end].decode()
        for key in ('hits', 'hits_en', 'hits_tech')
        for start, end in found[0][key] or ()
    }
    assert 'API_key' in terms
    assert not terms.intersection({'的', '了', 'the', '哇', 'please', ' '})
    assert earliest_hit(candidates[0].document, (found[0]['hits'], found[0]['hits_en'], found[0]['hits_tech'])) is not None


@pytest.mark.parametrize('query', ['不', '没有', '可以', '当前', '之前', '2026', 'not', 'if', 'can', 'one', '我', 'you', '得', '都', '只有', '必须', '就', 'no', 'or', 'all', 'we', 'this'])
def test_query_analyzer_preserves_task_bearing_words(repo, query):
    domain = identity('domain').replace(':', '_')
    caller = new(repo, domain)
    target = new(repo, domain)
    turn(repo, target.guard, query, answer='保留原文')
    scope = FrozenHistoryReadScope(caller.guard.session_id, domain, SessionContentRange(0))
    found, candidates = search(repo, scope, query, target_session_id=target.guard.session_id)
    assert len(candidates) == 1
    assert candidates[0].messages[0]['text'] == query
