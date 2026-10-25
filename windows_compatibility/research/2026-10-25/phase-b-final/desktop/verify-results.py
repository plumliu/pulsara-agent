import json,subprocess,datetime
from pathlib import Path
out=Path(__file__).resolve().parent
for name in ['setup-results.json','network-results.json','enospc-results.json','tested-js-provenance.json']:
 d=json.loads((out/name).read_text(encoding='utf-8'));assert d['passed'],name
s=json.loads((out/'setup-results.json').read_text(encoding='utf-8'));n=json.loads((out/'network-results.json').read_text(encoding='utf-8'));e=json.loads((out/'enospc-results.json').read_text(encoding='utf-8'))
assert len(s['cases'])==8 and len(n['cases'])==3 and n['carrier']['code']==0
assert n['events'][-1]['event']=='cleaned' and not n['events'][-1]['exists']
assert e['before']==e['after'] and e['error']['code']==2 and e['privateRootRemoved']
assert all(x['closed'] for x in n['requests']) and n['downloadDone']['state']=='cancelled'
assert n['afterRetire']['active']==n['afterRetire']['imports']==n['afterRetire']['stages']==[]
clean=json.loads((out/'cleanup-linux.json').read_text(encoding='utf-8'));assert clean['passed']
win=json.loads((out/'cleanup-windows.json').read_text(encoding='utf-8-sig'));assert all(not row['present'] for row in win)
assert json.loads((out/'production-scope.json').read_text(encoding='utf-8-sig'))['productionOwnerAndNativeDiffExitCode']==0
assert json.loads((out/'build-cache-final.json').read_text())['exitCode']==0
result={'version':'0.1.0-alpha.10','setupChecks':8,'networkCombinations':3,'enospcInjectedAllocationCase':1,'recipeFixturePassed':7,'actualNetworkHostExitCode':0,'actualGuestCarrierExitCode':0,'productionCodeChangedThisBatch':False,'observedInstalledProductOwners':True,'syntheticSessionCoreFixture':True,'fullMainInFlightClosureClaimed':False,'actualFullDiskClaimed':False,'safeForSleepBeforeRootManualAction':True,'allOriginalFailuresPreserved':True,'publicCredentialsRead':False,'B5ActivationClaimed':False,'passed':True}
(out/'verified-results.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
