"""Terminate only the failed hidden fixture's already recorded exact HANDLE."""
import ctypes as c
from ctypes import wintypes as w
import json
import sys
from pathlib import Path

root=Path.cwd()/'output/windows-b/host-setup'
record=root/(sys.argv[1] if len(sys.argv)>1 else 'readiness-first-process.json')
row=json.loads(record.read_text(encoding='utf-8-sig'))
k=c.WinDLL('kernel32',use_last_error=True)
k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
k.GetProcessTimes.argtypes=[w.HANDLE,c.POINTER(w.FILETIME),c.POINTER(w.FILETIME),c.POINTER(w.FILETIME),c.POINTER(w.FILETIME)]
k.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,c.POINTER(w.DWORD)]
k.TerminateProcess.argtypes=[w.HANDLE,w.UINT];k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];k.CloseHandle.argtypes=[w.HANDLE]
handle=k.OpenProcess(0x1000|0x100000|1,False,row['pid'])
if not handle:raise c.WinError(c.get_last_error())
try:
 times=[w.FILETIME() for _ in range(4)]
 assert k.GetProcessTimes(handle,*[c.byref(value) for value in times])
 creation=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
 print(json.dumps({'handleCreation':creation,'cimCreation':row['creation'],'delta':creation-row['creation']}))
 assert creation//10==row['creation']//10
 image=c.create_unicode_buffer(32768);size=w.DWORD(len(image))
 assert k.QueryFullProcessImageNameW(handle,0,image,c.byref(size))
 assert Path(image.value).resolve()==Path('desktop/node_modules/electron/dist/electron.exe').resolve()
 assert 'readiness-smoke.cjs' in row['command']
 assert k.TerminateProcess(handle,1)
 assert k.WaitForSingleObject(handle,10000)==0
 row.update(exactHandleCreation=creation,terminatedFixtureMain=True,reason='Fixture compared a string event name with the complete event object; prepared gate never released and no HEAD or operation submitted.')
 record.with_name(record.name.replace('-process','-cleanup')).write_text(json.dumps(row,indent=2)+'\n')
finally:k.CloseHandle(handle)
