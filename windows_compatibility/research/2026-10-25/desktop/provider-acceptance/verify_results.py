"""Read original real-provider rows; distinguish execution from semantic checks."""
import json
from pathlib import Path

output=Path(__file__).parent
artifact=json.loads((output/'artifact-result.json').read_text(encoding='utf-8'))
expected=artifact['expected']
assert artifact['status']=='completed' and artifact['undropped_databases']==[]
rows=[]
for case in artifact['cases']:
 name=case['case'];trace=case['trace'];reply=case['turns'][-1]['reply']
 starts=[t for t in trace if t['tool_name']=='terminal' and 'fixture_producer.py' in t['arguments']['command']]
 assert len(starts)==(2 if name=='multi' else 1)
 assert all(t['result_state']=='SUCCESS' for t in trace)
 assert case['existing_file_preserved']
 exports=[t['result'] for t in trace if t['tool_name']=='artifact_export']
 assert all(value['status']=='success' and value['bytes_written']>0 for value in exports)
 if name=='small':assert expected['small'] in reply
 elif name in {'json','existing'}:assert all(str(total) in reply.replace(',','') for total in expected['json'].values())
 elif name in {'log','invalid_json'}:
  assert expected['log'] in reply
  if name=='invalid_json':assert 'status={unfinished' in reply
 elif name=='multi':assert all(key in reply and value in reply for key,value in expected['multi'].items())
 elif name=='snapshot':
  assert exports and exports[0]['source_coverage']=='RETAINED_SNAPSHOT' and exports[0]['source_coverage_reason']=='TERMINAL_RETENTION_GAP'
  assert exports[0]['bytes_written']==65536 and 'retained' in reply.lower() and 'not' in reply.lower()
 else:assert 'alpha' in reply
 if name=='no_terminal':assert len([t for t in trace if t['tool_name']=='terminal'])==1
 if name=='readonly':assert not exports
 rows.append({'case':name,'primaryContractPassed':True,'toolCalls':len(trace),'producerExecutions':len(starts),'exports':exports,'occupiedFilePreserved':True,
  'qualification':"Snapshot primary gap/coverage conclusion is honest; final prose's incidental endpoint 01397 is inaccurate against visible source tail01399. No raw evidence rewritten." if name=='snapshot' else "Model avoided occupied.txt; this is preservation, not an induced no-clobber error." if name=='existing' else None})
for call in artifact['provider_calls']:
 tools=call['provider_input']['tools']
 assert any(t.get('function',{}).get('name',t.get('name'))=='artifact_export' for t in tools)
results={'artifact':{'status':'original runner completed; 9 primary contracts independently checked','cases':rows,'providerCalls':len(artifact['provider_calls']),'fullProductionExportToolAdvertised':True},'other':{}}
for name in ('memory','history','interruption','crash'):
 path=output/(name+'-result.json')
 if not path.exists():continue
 value=json.loads(path.read_text(encoding='utf-8'))
 results['other'][name]={'status':value['status'],'providerCalls':len(value['provider_calls']),'prefixInstallations':len(value.get('prefix_checks',[])),'sameEpochComparisons':sum(row['compared'] for row in value.get('prefix_checks',[])),'undroppedDatabases':value['undropped_databases']}
 assert value['status']=='completed' and value['undropped_databases']==[]
 if name in {'memory','crash'}:assert value['cases'][0]['passed']
results['executionBoundaries']='Original provider/owner loops; output-only PG/home/recording adapters. UserStop is distinct from OS Host loss.'
seed=json.loads((output/'crash-seed.json').read_text(encoding='utf-8'))
child_checks=0
for left,right in zip(seed['calls'],seed['calls'][1:]):
 a,b=left['provider_input'],right['provider_input']
 assert json.dumps(a['tools'],ensure_ascii=False)==json.dumps(b['tools'],ensure_ascii=False)
 assert b['messages'][:len(a['messages'])]==a['messages']
 if 'system' in a:assert a['system']==b['system']
 child_checks+=1
results['crashChildRecordedWirePrefixComparisons']=child_checks
(output/'verified-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'artifactPrimaryCases':len(rows),'other':results['other']},ensure_ascii=False))
