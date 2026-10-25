"""Hold an exact Windows process HANDLE until exit; never signal the process."""
import argparse
import ctypes as c
from ctypes import wintypes as w
from datetime import datetime, timezone
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    ready = args.output / 'child-handle-ready.json'
    done = args.output / 'child-handle-exited.json'
    assert not ready.exists() and not done.exists()
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.GetProcessTimes.argtypes = [w.HANDLE, *([c.POINTER(w.FILETIME)] * 4)]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, args.pid)
    if not handle:
        raise c.WinError(c.get_last_error())
    try:
        times = [w.FILETIME() for _ in range(4)]
        assert kernel.GetProcessTimes(handle, *[c.byref(value) for value in times])
        image = c.create_unicode_buffer(32768)
        size = w.DWORD(len(image))
        assert kernel.QueryFullProcessImageNameW(handle, 0, image, c.byref(size))
        assert Path(image.value).resolve() == args.image.resolve()
        assert kernel.WaitForSingleObject(handle, 0) == 258
        facts = {'pid': args.pid, 'image': image.value,
                 'creationFileTime': (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
                 'observedAt': datetime.now(timezone.utc).isoformat(),
                 'alive': True, 'signalSent': False}
        ready.write_text(json.dumps(facts, indent=2), encoding='utf-8')
        # This observer has no task-lifetime timeout; the original product owns exit.
        assert kernel.WaitForSingleObject(handle, 0xffffffff) == 0
        code = w.DWORD()
        assert kernel.GetExitCodeProcess(handle, c.byref(code))
        done.write_text(json.dumps({**facts, 'alive': False, 'exitCode': code.value,
                                    'exitObservedAt': datetime.now(timezone.utc).isoformat(),
                                    'sameHeldHandle': True}, indent=2), encoding='utf-8')
    finally:
        assert kernel.CloseHandle(handle)


if __name__ == '__main__':
    main()
