"""Execute the real-PG post-accept counterexample against the final wheel."""
from __future__ import annotations
import ast
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
import json
import os
from pathlib import Path
import sys
import traceback
from unittest.mock import patch

import pulsara_agent
REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
product = Path(pulsara_agent.__file__).resolve()
assert product.is_relative_to(REPO/'desktop/resources/python') and 'site-packages' in product.parts
sys.path.append(str(REPO))

import psycopg
from pulsara_agent.conversation_kernel.provider_dispatch import ProviderDispatchCoordinator
from tests.support.postgres_database import create_migrated_postgres_test_database, drop_postgres_test_database

# pytest is intentionally absent from the product runtime. Execute only the
# unmodified original counterexample/helper definitions, using their original
# imports except pytest; do not install dev packages or mix venv dependencies.
source_path=REPO/'tests/test_round5_long_horizon_postgres.py'
selected={'_name','_text_stream','_KnownReadOnlyTool','_runner','_lease','_prepare_subagent_task',
          'test_root_child_admission_after_root_accept_reprepares_only_the_accepted_turn'}
tree=ast.parse(source_path.read_text(),filename=str(source_path))
nodes=[node for node in tree.body if
       (isinstance(node,(ast.Import,ast.ImportFrom)) and
        not (isinstance(node,ast.Import) and any(alias.name=='pytest' for alias in node.names)))
       or (isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and node.name in selected)]
assert {node.name for node in nodes if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))}==selected
definitions={}
exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source_path),'exec'),definitions)
counterexample=definitions['test_root_child_admission_after_root_accept_reprepares_only_the_accepted_turn']


def diff(a, b, prefix=''):
    if a == b:
        return []
    if type(a) is type(b) and is_dataclass(a):
        return [item for field in fields(a) for item in diff(getattr(a,field.name),getattr(b,field.name),prefix+'.'+field.name)]
    if isinstance(a,tuple) and isinstance(b,tuple) and len(a)==len(b):
        return [item for index,(x,y) in enumerate(zip(a,b)) for item in diff(x,y,prefix+f'[{index}]')]
    return [{'field':prefix,'prepared':repr(a),'actual':repr(b)}]


facts=json.loads((REPO/'output/macos-regression-20261025/root/private-pg.json').read_text())
report={'product_import':str(product),'private_cluster':facts['identity'],'activation_differences':[], 'retired':[],
        'original_test_definitions_reused':sorted(selected),'pytest_not_in_product_runtime':True}
expected=ContextVar('counterexample_expected',default=None)
original_activate=ProviderDispatchCoordinator.activate_prospective_root_input
original_read=ProviderDispatchCoordinator.read_dispatch_read

async def activate(owner, prepared, **kwargs):
    token=expected.set(prepared.admission.canonical_read)
    old=owner._continuity.current_view(prepared.planning.scope)
    try:
        result=await original_activate(owner,prepared,**kwargs)
        if result is None:
            assert not prepared.owns_resources
            assert owner._safe_point._active_handle is None
            assert owner._continuity.current_view(prepared.planning.scope) is old
            report['retired'].append({'turn_id':prepared.admission.candidate.exact_turn_id,
                'resources_retired':True,'safe_point_handle_retired':True,'installed_view_object_unchanged':True})
        return result
    finally:
        expected.reset(token)

async def read(owner,cut,**kwargs):
    actual=await original_read(owner,cut,**kwargs)
    before=expected.get()
    if before is not None and before!=actual:
        report['activation_differences'].append(diff(before,actual))
    return actual

database=None
try:
    with patch.dict(os.environ,{'PULSARA_POSTGRES_ADMIN_DSN':facts['admin_dsn'],
                                'PULSARA_POSTGRES_DSN':facts['runtime_dsn'],'CI':'true'}):
        database=create_migrated_postgres_test_database()
        report['database']=database.database_name
        with patch.object(ProviderDispatchCoordinator,'activate_prospective_root_input',activate), \
             patch.object(ProviderDispatchCoordinator,'read_dispatch_read',read):
            counterexample(database)
        assert len(report['activation_differences'])==1
        assert len(report['retired'])==1
        differences=report['activation_differences'][0]
        assert {d['field'] for d in differences}=={
            '.compile_snapshot.canonical_input.identity.provider_input_through_sequence',
            '.compile_snapshot.canonical_input.identity.identity_fingerprint',
            '.compile_snapshot.canonical_input.snapshot_fingerprint',
            '.compile_snapshot.canonical_read_cut_fingerprint',
            '.replay_manifest_cut.provider_input_through_sequence',
            '.replay_manifest_cut.cut_fingerprint','.composite_fingerprint'}
        report['status']='passed'
except BaseException as error:
    report.update(status='failed',error_type=type(error).__name__,error=str(error),traceback=traceback.format_exc())
finally:
    if database is not None:
        drop_postgres_test_database(facts['admin_dsn'],database.database_name)
        with psycopg.connect(facts['admin_dsn'],autocommit=True) as connection:
            assert connection.execute('SELECT count(*) FROM pg_database WHERE datname=%s',(database.database_name,)).fetchone()[0]==0
        report['database_dropped']=True
    encoded=json.dumps(report,ensure_ascii=False,indent=2)
    (OUT/'final-wheel-counterexample.json').write_text(encoded+'\n')
    print(encoded)
raise SystemExit(0 if report['status']=='passed' else 2)
