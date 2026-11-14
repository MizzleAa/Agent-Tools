"""자식 프로세스 정리: 프로세스 트리 통째로 죽이기 + '관리 서버가 죽으면 같이 죽는' Windows Job.

콘솔 창을 강제로 닫으면 파이썬은 정리 코드를 돌릴 틈 없이 끝난다. 그때 남는 것이
- llama-server (창 없이 띄웠으므로 콘솔과 무관하게 계속 돈다)
- 에이전트가 run_command / run_python 으로 띄운 프로그램 (GUI 앱, 긴 스크립트 등)
이다. 둘 다 이 Job 에 넣어 두면, 이 프로세스의 Job 핸들이 닫히는 순간(= 어떤 식으로든 끝나는 순간)
Windows 가 Job 안의 프로세스와 그 자식들을 모두 끝낸다.
"""

from __future__ import annotations

import ctypes
import os
import threading

import psutil


def kill_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(procs, timeout=5)


class _JobLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JobLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class KillOnCloseJob:
    """핸들이 닫히면(= 이 프로세스가 어떤 식으로든 끝나면) 안에 넣은 프로세스를 모두 죽이는 Job."""
    KILL_ON_JOB_CLOSE = 0x2000
    EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.k32 = k32
        handle = k32.CreateJobObjectW(None, None)
        info = _JobExtendedLimits()
        info.BasicLimitInformation.LimitFlags = self.KILL_ON_JOB_CLOSE
        if handle and k32.SetInformationJobObject(handle, self.EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
            self.handle = handle

    def add(self, pid: int) -> bool:
        if not self.handle:
            return False
        proc = self.k32.OpenProcess(0x0100 | 0x0001, False, pid)   # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not proc:
            return False
        try:
            return bool(self.k32.AssignProcessToJobObject(self.handle, proc))
        finally:
            self.k32.CloseHandle(proc)

    def close(self) -> None:
        if self.handle:
            self.k32.CloseHandle(self.handle)
            self.handle = None


_job: KillOnCloseJob | None = None
_lock = threading.Lock()


def job_add(pid: int) -> bool:
    """이 프로세스(관리 서버·CLI)가 끝날 때 pid 도 같이 끝나게 한다. 자식이 띄우는 손자 프로세스도 따라 들어간다."""
    global _job
    with _lock:
        if _job is None or not _job.handle:
            _job = KillOnCloseJob()
        return _job.add(pid)
