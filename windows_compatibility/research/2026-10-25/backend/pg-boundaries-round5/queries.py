"""Exercise clean-v0 and the real PG search API in the probe's Unix-socket DB."""

import json
import os
from pathlib import Path
import sys

import psycopg
from pulsara_agent.desktop_runtime import prepare_database, database_diagnostic
from pulsara_agent.retrieval.pg_search import (
    lexical_predicate,
    lexical_parameters,
    _TOKENIZERS,
)
from pulsara_agent.settings import LocalPostgresConfig

home = Path(sys.argv[1]).resolve(strict=True)
assert (
    home.parent == Path("/home/plumwsl")
    and home.name.startswith(".g0p5-")
    and home.stat().st_uid == os.getuid()
)
os.environ["PULSARA_HOME"] = str(home)
socket = home / "desktop/socket"
settings = LocalPostgresConfig(
    runtime_dsn=f"host='{socket}' port=5432 dbname=pulsara user=pulsara_runtime",
    admin_dsn=f"host='{socket}' port=5432 dbname=pulsara user=pulsara_admin",
)
if sys.argv[2] == "initial":
    prepare_database(settings, initialize=True)
prepare_database(settings, initialize=False)
print(
    json.dumps(
        {
            "scenario": "clean-v0-verify",
            "result": database_diagnostic(
                settings, command="verify", deadline_seconds=300, deep=True
            ),
        },
        ensure_ascii=False,
    ),
    flush=True,
)
with psycopg.connect(settings.admin_dsn, autocommit=True) as connection:
    facts = connection.execute(
        "SELECT current_user, current_database(), current_setting('server_version'), current_setting('data_directory'), current_setting('unix_socket_directories'), current_setting('listen_addresses')"
    ).fetchone()
    assert (
        facts[2] == "17.9"
        and facts[3] == str(home / "desktop/postgres")
        and facts[4] == str(socket)
        and facts[5] == ""
    )
    roles = connection.execute(
        "SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname IN ('pulsara_admin','pulsara_runtime') ORDER BY rolname"
    ).fetchall()
    assert roles[1] == ("pulsara_runtime", False, False, False, False, False, True)
    versions = dict(
        connection.execute(
            "SELECT extname,extversion FROM pg_extension WHERE extname IN ('vector','pg_search')"
        ).fetchall()
    )
    assert versions == {"vector": "0.8.1", "pg_search": "0.26.1"}
    print(
        json.dumps(
            {
                "scenario": "pg-facts",
                "facts": facts,
                "roles": roles,
                "extensions": versions,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if sys.argv[2] == "initial":
        connection.execute(
            "CREATE TABLE public.g0_documents (id bigint PRIMARY KEY, body text, body_en text GENERATED ALWAYS AS (body) STORED, body_tech text GENERATED ALWAYS AS (body) STORED, embedding vector(3))"
        )
        for identity, text in enumerate(
            (
                "中文搜索与持久化数据库",
                "desktop portability running checks",
                "src/pulsara_agent/desktop_runtime.py prepare_database",
                "unrelated cobalt",
            ),
            1,
        ):
            connection.execute(
                "INSERT INTO public.g0_documents(id,body,embedding) VALUES (%s,%s,'[1,2,3]')",
                (identity, text),
            )
        fields = json.dumps(
            {
                name: {"tokenizer": tokenizer}
                for name, tokenizer in zip(
                    ("body", "body_en", "body_tech"), _TOKENIZERS
                )
            },
            ensure_ascii=False,
        )
        from psycopg import sql

        connection.execute(
            sql.SQL(
                "CREATE INDEX g0_documents_bm25 ON public.g0_documents USING bm25(id,body,body_en,body_tech) WITH(key_field='id',text_fields={})"
            ).format(sql.Literal(fields))
        )
    for query, expected in (("数据库", 1), ("running", 2), ("prepare_database", 3)):
        rows = connection.execute(
            f"SELECT id,body,paradedb.snippet_positions(body),paradedb.snippet_positions(body_en),paradedb.snippet_positions(body_tech) FROM public.g0_documents WHERE {lexical_predicate('body')} ORDER BY id",
            lexical_parameters(query),
        ).fetchall()
        assert [row[0] for row in rows] == [expected], (query, rows)
        assert any(rows[0][2:]), (query, rows)
        print(
            json.dumps(
                {"scenario": "bm25-snippet", "query": query, "rows": rows},
                ensure_ascii=False,
            ),
            flush=True,
        )
    distance = connection.execute(
        "SELECT embedding <-> '[1,2,3]'::vector FROM public.g0_documents WHERE id=1"
    ).fetchone()[0]
    assert distance == 0
    print(json.dumps({"scenario": "vector", "distance": distance}), flush=True)
print(json.dumps({"scenario": "queries-passed"}), flush=True)
