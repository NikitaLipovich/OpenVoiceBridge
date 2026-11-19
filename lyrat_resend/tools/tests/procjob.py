"""Processes started by a test run die with it: Windows job objects through ctypes, no new packages. Goal 107.

A run left a live s3_sim.py holding ota_test/build/otahost.dll after a failed check, and the next build of the DLL
failed with LNK1104 (goal 106 #001). subprocess kills only the child it started, never the child's children, and a
parent killed from outside kills nothing. A job object with KILL_ON_JOB_CLOSE holds a process and every process it
starts after it (children inherit the job): terminate the job and the whole tree goes; close the last handle of the
job, which Windows does by itself when the process holding it dies by any means, and the tree goes too.

  Job()                      a new job, KILL_ON_JOB_CLOSE; .pids() what still runs in it, .terminate() (returns when
                             the processes are gone), .cpu_seconds() of all it held, .close()
  popen(job, cmd, **kw)      subprocess.Popen started suspended, put into the job, then resumed: no grandchild escapes
  tie_this_process()         this process and all it starts from now on die when it dies (for tests run alone)
  pid_alive(pid)             whether a process with this pid runs

Only processes in our own job are ever killed: never by name, never by port. Off Windows the calls do nothing.
Based on bench/btlog.py die_with_this_process (goal 055).
"""
import ctypes
import subprocess
import sys
import time

WINDOWS = sys.platform == "win32"

if WINDOWS:
    from ctypes import wintypes

    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _nt = ctypes.WinDLL("ntdll")
    _k.CreateJobObjectW.restype = wintypes.HANDLE
    _k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.c_void_p]
    _k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k.CloseHandle.argtypes = [wintypes.HANDLE]
    _k.OpenProcess.restype = wintypes.HANDLE
    _k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k.GetCurrentProcess.restype = wintypes.HANDLE
    _nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k.WaitForSingleObject.restype = wintypes.DWORD

CREATE_SUSPENDED = 0x00000004
KILL_ON_JOB_CLOSE = 0x00002000
LIMIT_PRIORITY_CLASS = 0x00000020
BELOW_NORMAL = 0x00004000        # BELOW_NORMAL_PRIORITY_CLASS
ABOVE_NORMAL = 0x00008000        # ABOVE_NORMAL_PRIORITY_CLASS
JOB_EXTENDED_LIMITS = 9          # JobObjectExtendedLimitInformation
JOB_PID_LIST = 3                 # JobObjectBasicProcessIdList
JOB_ACCOUNTING = 1               # JobObjectBasicAccountingInformation
STILL_ACTIVE = 259
SYNCHRONIZE = 0x00100000
QUERY_LIMITED = 0x1000


def _err(what):
    return OSError(ctypes.get_last_error(), "%s: %s" % (what, ctypes.FormatError(ctypes.get_last_error())))


class Job:
    """A job object that kills what is in it when its last handle closes."""

    def __init__(self, priority_class=None):
        """priority_class (BELOW_NORMAL, ABOVE_NORMAL): every process of the job runs at it, its children too (goal 110)."""
        self.handle = None
        if not WINDOWS:
            return
        h = _k.CreateJobObjectW(None, None)
        if not h:
            raise _err("CreateJobObject")
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION: LimitFlags at offset 16 on 64-bit and 8 on 32-bit, PriorityClass at 56/32
        size = 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112
        info = (ctypes.c_byte * size)()
        flags = KILL_ON_JOB_CLOSE | (LIMIT_PRIORITY_CLASS if priority_class else 0)
        ctypes.cast(ctypes.byref(info, 16 if size == 144 else 8), ctypes.POINTER(wintypes.DWORD))[0] = flags
        if priority_class:
            ctypes.cast(ctypes.byref(info, 56 if size == 144 else 32), ctypes.POINTER(wintypes.DWORD))[0] = priority_class
        if not _k.SetInformationJobObject(h, JOB_EXTENDED_LIMITS, info, size):
            _k.CloseHandle(h)
            raise _err("SetInformationJobObject")
        self.handle = h

    def assign(self, process_handle):
        if self.handle and not _k.AssignProcessToJobObject(self.handle, process_handle):
            raise _err("AssignProcessToJobObject")

    def pids(self):
        """Pids of the processes still running in the job."""
        if not self.handle:
            return []
        n = 1024
        while True:
            # JOBOBJECT_BASIC_PROCESS_ID_LIST: two DWORDs, then ULONG_PTR ids
            buf = (ctypes.c_byte * (8 + n * ctypes.sizeof(ctypes.c_size_t)))()
            if _k.QueryInformationJobObject(self.handle, JOB_PID_LIST, buf, len(buf), None):
                break
            if ctypes.get_last_error() != 234 or n > 1 << 16:      # ERROR_MORE_DATA
                raise _err("QueryInformationJobObject")
            n *= 4
        count = ctypes.cast(ctypes.byref(buf, 4), ctypes.POINTER(wintypes.DWORD))[0]
        ids = ctypes.cast(ctypes.byref(buf, 8), ctypes.POINTER(ctypes.c_size_t * count))[0]
        return [int(i) for i in ids]

    def terminate(self, code=1, wait=10.0):
        """Kill every process in the job and wait (up to `wait` s) till they are gone - TerminateJobObject only starts
        it, and on a busy PC a process can take more than a second to go (goal 110). Returns the pids that ran."""
        left = self.pids()
        if self.handle and left:
            # Handles first: a process is gone when its handle is signalled, not when the job's list drops it.
            handles = [h for h in (_k.OpenProcess(SYNCHRONIZE | QUERY_LIMITED, False, p) for p in left) if h]
            _k.TerminateJobObject(self.handle, code)
            end = time.monotonic() + wait
            for h in handles:
                _k.WaitForSingleObject(h, max(0, int((end - time.monotonic()) * 1000)))
                _k.CloseHandle(h)
            while self.pids() and time.monotonic() < end:
                time.sleep(0.02)
        return left

    def cpu_seconds(self):
        """User + kernel time of every process the job ever held, ended ones too (goal 110)."""
        if not self.handle:
            return None
        info = (ctypes.c_int64 * 6)()       # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: 4 times, then 4 DWORDs
        if not _k.QueryInformationJobObject(self.handle, JOB_ACCOUNTING, info, ctypes.sizeof(info), None):
            return None
        return round((info[0] + info[1]) / 1e7, 1)

    def close(self):
        """Close our handle: with no other handle open, whatever still runs in the job is killed."""
        if self.handle:
            _k.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.terminate()
        self.close()


def popen(job, cmd, **kw):
    """Popen whose process is in the job before it runs a single instruction, so all it starts is in the job too."""
    if not WINDOWS or job is None or not job.handle:
        return subprocess.Popen(cmd, **kw)
    kw["creationflags"] = kw.get("creationflags", 0) | CREATE_SUSPENDED
    p = subprocess.Popen(cmd, **kw)
    try:
        job.assign(int(p._handle))
    except OSError:
        p.kill()
        p.wait()
        raise
    finally:
        _nt.NtResumeProcess(int(p._handle))
    return p


_tied = None


def tie_this_process():
    """Put this process in a KILL_ON_JOB_CLOSE job held only by itself: all it starts from now dies when it dies,
    even by TerminateProcess. Once per process; under run_tests the job nests in the check's job."""
    global _tied
    if _tied is None and WINDOWS:
        job = Job()
        job.assign(_k.GetCurrentProcess())
        _tied = job          # never closed: the handle goes with the process
    return _tied


def pid_alive(pid):
    if not WINDOWS:
        import os
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    h = _k.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        return bool(_k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        _k.CloseHandle(h)
