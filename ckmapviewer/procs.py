"""Windows process helpers (ctypes, no extra dependencies).

* find running CoreKeeperServer.exe processes and their executable paths (to refuse mod changes
  while the real server runs);
* tie a child process to this app with a Job Object (kill-on-close), so the disposable server
  used for full-map generation can never outlive the viewer;
* stop a process WE started, by PID only.
"""
import os
import subprocess
import sys
from pathlib import Path

from .paths import norm_key

IS_WIN = sys.platform == "win32"

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4


def _image_path(pid: int):
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = wintypes.DWORD(len(buf))
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
        return None
    finally:
        _k32.CloseHandle(h)


def process_info(pid: int):
    """None if no such process is running; else {"exe": path or None, "created": epoch or None}.
    exe/created are None when the process exists but cannot be inspected (access denied):
    callers must then treat it as alive and unknown."""
    pid = int(pid)
    if pid <= 0:
        return None
    if not IS_WIN:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return {"exe": None, "created": None}
        return {"exe": None, "created": None}
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        err = ctypes.get_last_error()
        if err == 87:          # ERROR_INVALID_PARAMETER: no such process
            return None
        return {"exe": None, "created": None}   # e.g. access denied: exists, unknown
    try:
        code = wintypes.DWORD()
        if _k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:   # STILL_ACTIVE
            return None
        buf = ctypes.create_unicode_buffer(32768)
        n = wintypes.DWORD(len(buf))
        exe = buf.value if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
        created = None
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            ft = (c.dwHighDateTime << 32) | c.dwLowDateTime
            created = ft / 1e7 - 11644473600.0
        return {"exe": exe, "created": created}
    finally:
        _k32.CloseHandle(h)


def find_processes(exe_name: str):
    """[(pid, full exe path or None)] for every running process with that file name.
    Returns None if the check itself failed (callers must then refuse, not assume 'not running')."""
    if not IS_WIN:
        return None
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return None
    out = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == exe_name.lower():
                out.append((int(e.th32ProcessID), _image_path(int(e.th32ProcessID))))
            ok = _k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return out


def server_running_from(install_dir: Path, exe_name="CoreKeeperServer.exe") -> dict:
    """Is a CoreKeeperServer.exe from exactly this install folder running?

    {"running": bool|None, "pids": [...], "unknown": [...]}. running=None: could not check.
    A process whose path cannot be read (e.g. elevated / other user) counts as 'unknown' and
    callers treat it as possibly running.
    """
    procs = find_processes(exe_name)
    if procs is None:
        return {"running": None, "pids": [], "unknown": []}
    want = norm_key(Path(install_dir) / exe_name)
    pids = [pid for pid, p in procs if p and norm_key(p) == want]
    unknown = [pid for pid, p in procs if not p]
    return {"running": bool(pids), "pids": pids, "unknown": unknown}


# ---------------------------------------------------------------------------- job object
class KillOnCloseJob:
    """Every process assigned to this job is terminated by Windows when this app exits (any way)."""

    def __init__(self):
        self.handle = None
        if not IS_WIN:
            return
        try:
            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                            ("PerJobUserTimeLimit", ctypes.c_longlong),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class EXTENDED(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            _k32.CreateJobObjectW.restype = wintypes.HANDLE
            h = _k32.CreateJobObjectW(None, None)
            if not h:
                return
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            if not _k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                _k32.CloseHandle(h)
                return
            self.handle = h
        except (OSError, AttributeError):
            self.handle = None

    def assign(self, proc: subprocess.Popen) -> bool:
        if not self.handle:
            return False
        try:
            _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            return bool(_k32.AssignProcessToJobObject(self.handle, int(proc._handle)))
        except (OSError, AttributeError):
            return False


_JOB = None


def job() -> KillOnCloseJob:
    global _JOB
    if _JOB is None:
        _JOB = KillOnCloseJob()
    return _JOB


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def stop_own_process(proc: subprocess.Popen, grace=90):
    """Graceful close first (lets the server save), then force - only this PID (and its children)."""
    if proc.poll() is not None:
        return
    if IS_WIN:
        subprocess.call(["taskkill", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        if IS_WIN:
            subprocess.call(["taskkill", "/PID", str(proc.pid), "/F", "/T"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        else:
            proc.kill()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass


def open_folder(path: Path):
    if IS_WIN:
        os.startfile(str(path))  # noqa: S606 - only folders the app itself manages
