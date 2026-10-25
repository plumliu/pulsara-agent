import json
from pathlib import Path
out=Path(__file__).resolve().parent
read=lambda n:json.loads((out/n).read_text(encoding='utf-8-sig'))
target=read('explorer-created-target.json');name=target['folderName'];before=(out/'explorer-picker-before.txt').read_text(encoding='utf-8');stale=(out/'explorer-picker-stale-before-refresh.txt').read_text(encoding='utf-8');after=(out/'explorer-picker-after-refresh.txt').read_text(encoding='utf-8');selected=(out/'explorer-path-selected.txt').read_text(encoding='utf-8')
assert name not in before and name not in stale and name in after and target['linux'] in selected
facts=json.loads((out/'explorer-verify.log').read_text(encoding='utf-8'));assert facts['passed'] and facts['canonical']==target['linux'] and facts['empty']
quit=read('explorer-normal-quit.json');assert quit['exactInstalledAppCount']==0 and not quit['externalKillUsed']
result={'passed':True,'requirement':'spec13.2 Explorer新建后刷新','target':target,'linuxIdentity':facts,'pickerInitiallyAbsent':True,'pickerStaleUntilUserRefresh':True,'pickerThenListedAndSelectedSameCanonical':True,'sessionNotCreated':True,'modelCalls':0,'settingsActionPerformed':False,'normalQuit':quit,'createdEmptyDirectoryPreserved':True,'ownExplorerWindowClosed':True,'automationLimitations':['two coordinate input geometry unavailable errors; refreshed/activated then proceeded','Explorer accessibility focus stale while inline name visible; used supported set_value on observed name control','new-session close click did not dismiss immediately; Escape dismissed, no create action']}
(out/'explorer-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print('Explorer required case PASS; directory inode',facts['inode'])
