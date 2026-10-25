"""Read-only native display/HKL observation; no display/input changes."""
import argparse
import ctypes as c
from ctypes import wintypes as w
from datetime import datetime, timezone
import json
import os
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pulsara-pid',type=int,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();assert not args.output.exists()
    user=c.WinDLL('user32',use_last_error=True)
    shcore=c.WinDLL('shcore',use_last_error=True)
    kernel=c.WinDLL('kernel32',use_last_error=True)
    shcore.SetProcessDpiAwareness.argtypes=[c.c_int]
    shcore.SetProcessDpiAwareness.restype=c.c_long
    # This setting affects only this new observer process, never Windows display settings.
    assert shcore.SetProcessDpiAwareness(2)==0
    awareness=c.c_int()
    shcore.GetProcessDpiAwareness.argtypes=[w.HANDLE,c.POINTER(c.c_int)]
    assert shcore.GetProcessDpiAwareness(None,c.byref(awareness))==0 and awareness.value==2

    class Info(c.Structure):
        _fields_=[('size',w.DWORD),('monitor',w.RECT),('work',w.RECT),('flags',w.DWORD),('device',w.WCHAR*32)]
    user.GetMonitorInfoW.argtypes=[w.HANDLE,c.POINTER(Info)]
    shcore.GetDpiForMonitor.argtypes=[w.HANDLE,c.c_int,c.POINTER(w.UINT),c.POINTER(w.UINT)]
    shcore.GetDpiForMonitor.restype=c.c_long
    monitors=[];errors=[]
    callback_type=c.WINFUNCTYPE(w.BOOL,w.HANDLE,w.HDC,c.POINTER(w.RECT),w.LPARAM)
    @callback_type
    def monitor_callback(handle,hdc,rect,data):
        try:
            info=Info();info.size=c.sizeof(info)
            assert user.GetMonitorInfoW(handle,c.byref(info))
            x=w.UINT();y=w.UINT()
            assert shcore.GetDpiForMonitor(handle,0,c.byref(x),c.byref(y))==0
            r=info.monitor
            monitors.append({'display':info.device,'primary':bool(info.flags&1),
                'resolutionPixels':[r.right-r.left,r.bottom-r.top],
                'desktopRectPixels':[r.left,r.top,r.right,r.bottom],
                'effectiveDpi':[x.value,y.value],'effectiveScalePercent':[x.value/96*100,y.value/96*100]})
        except Exception as error:errors.append(repr(error));return False
        return True
    user.EnumDisplayMonitors.argtypes=[w.HDC,c.POINTER(w.RECT),callback_type,w.LPARAM]
    assert user.EnumDisplayMonitors(None,None,monitor_callback,0) and not errors,errors

    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,c.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    process=kernel.OpenProcess(0x1000,False,args.pulsara_pid);assert process
    try:
        text=c.create_unicode_buffer(32768);length=w.DWORD(len(text))
        assert kernel.QueryFullProcessImageNameW(process,0,text,c.byref(length))
        assert Path(text.value).resolve()==(Path(os.environ['LOCALAPPDATA'])/'Programs/pulsara/pulsara.exe').resolve()
    finally:kernel.CloseHandle(process)
    user.GetKeyboardLayoutList.argtypes=[c.c_int,c.POINTER(w.HANDLE)]
    user.GetKeyboardLayoutList.restype=c.c_int
    count=user.GetKeyboardLayoutList(0,None);assert count>0
    layouts=(w.HANDLE*count)();count=user.GetKeyboardLayoutList(count,layouts);assert count>0
    user.GetKeyboardLayout.argtypes=[w.DWORD];user.GetKeyboardLayout.restype=w.HANDLE
    user.GetWindowThreadProcessId.argtypes=[w.HWND,c.POINTER(w.DWORD)];user.GetWindowThreadProcessId.restype=w.DWORD
    user.IsWindowVisible.argtypes=[w.HWND]
    user.IsIconic.argtypes=[w.HWND]
    user.GetWindowRect.argtypes=[w.HWND,c.POINTER(w.RECT)]
    user.MonitorFromWindow.argtypes=[w.HWND,w.DWORD];user.MonitorFromWindow.restype=w.HANDLE
    user.GetDpiForWindow.argtypes=[w.HWND];user.GetDpiForWindow.restype=w.UINT
    windows=[]
    window_callback_type=c.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    @window_callback_type
    def window_callback(hwnd,data):
        pid=w.DWORD();thread=user.GetWindowThreadProcessId(hwnd,c.byref(pid))
        if pid.value==args.pulsara_pid and user.IsWindowVisible(hwnd):
            try:
                layout=user.GetKeyboardLayout(thread)
                rect=w.RECT();assert user.GetWindowRect(hwnd,c.byref(rect))
                monitor=user.MonitorFromWindow(hwnd,2);assert monitor
                info=Info();info.size=c.sizeof(info);assert user.GetMonitorInfoW(monitor,c.byref(info))
                x=w.UINT();y=w.UINT();assert shcore.GetDpiForMonitor(monitor,0,c.byref(x),c.byref(y))==0
                dpi=user.GetDpiForWindow(hwnd);assert dpi
                windows.append({'threadId':thread,'hkl':hex(layout),'languageIdHex':f'{layout&0xffff:04x}',
                    'windowRectPixels':[rect.left,rect.top,rect.right,rect.bottom],
                    'minimized':bool(user.IsIconic(hwnd)),
                    'monitorDisplay':info.device,'monitorEffectiveDpi':[x.value,y.value],
                    'windowDpi':dpi,'windowScalePercent':dpi/96*100})
            except Exception as error:errors.append(repr(error));return False
        return True
    user.EnumWindows.argtypes=[window_callback_type,w.LPARAM]
    assert user.EnumWindows(window_callback,0) and not errors,errors
    result={'utc':datetime.now(timezone.utc).isoformat(),'activeDisplayCount':len(monitors),'monitors':monitors,
        'observerProcessDpiAwareness':awareness.value,'loadedHkl':[hex(layouts[i]) for i in range(count)],
        'pulsaraPid':args.pulsara_pid,'visiblePulsaraWindowThreadLayouts':windows,
        'deviceSerialNumbersCollected':False,'displayOrInputSettingsChanged':False,'uiControlled':False,
        'altGrSemantics':'HKL values and a right Alt key do not alone prove physical AltGr character behavior.'}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
