"""Run the native per-user installer with ownership of its complete process tree.

Inno's loader can exit before its temporary Setup child. Waiting for, or killing,
only the loader is therefore insufficient before replacing installation files.
The child starts suspended, joins a non-breakaway Job Object, and only then runs.
No process is selected by name, executable directory, or a recycled PID.

Windows contracts used here:
https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information
"""
from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import subprocess
import time


class InstallerProcessError(RuntimeError):
    """Rollback is allowed only when shutdown_confirmed is explicitly true."""

    def __init__(self, message: str, *, shutdown_confirmed: bool = False):
        super().__init__(message)
        self.shutdown_confirmed = shutdown_confirmed


class InstallerTimeoutError(InstallerProcessError):
    """The installer exceeded its deadline and its whole job has stopped."""


# Fixed-width Windows types also make the ABI declarations inspectable on POSIX.
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", DWORD), ("lpReserved", ctypes.c_wchar_p),
                ("lpDesktop", ctypes.c_wchar_p), ("lpTitle", ctypes.c_wchar_p),
                ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD),
                ("dwYSize", DWORD), ("dwXCountChars", DWORD),
                ("dwYCountChars", DWORD), ("dwFillAttribute", DWORD),
                ("dwFlags", DWORD), ("wShowWindow", WORD), ("cbReserved2", WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
                ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


class _BasicLimitInfo(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", DWORD),
                ("MinimumWorkingSetSize", SIZE_T), ("MaximumWorkingSetSize", SIZE_T),
                ("ActiveProcessLimit", DWORD), ("Affinity", SIZE_T),
                ("PriorityClass", DWORD), ("SchedulingClass", DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimitInfo(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimitInfo), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
                ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


class _AccountingInfo(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", DWORD), ("TotalProcesses", DWORD),
                ("ActiveProcesses", DWORD), ("TotalTerminatedProcesses", DWORD)]


class _WindowsJob:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Installer process supervision requires Windows")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], HANDLE),
            "SetInformationJobObject": ([HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD], ctypes.c_int32),
            "QueryInformationJobObject": ([HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.c_void_p], ctypes.c_int32),
            "CreateProcessW": ([ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_int32, DWORD, ctypes.c_void_p,
                                ctypes.c_wchar_p, ctypes.POINTER(_StartupInfo),
                                ctypes.POINTER(_ProcessInfo)], ctypes.c_int32),
            "AssignProcessToJobObject": ([HANDLE, HANDLE], ctypes.c_int32),
            "ResumeThread": ([HANDLE], DWORD),
            "WaitForSingleObject": ([HANDLE, DWORD], DWORD),
            "GetExitCodeProcess": ([HANDLE, ctypes.POINTER(DWORD)], ctypes.c_int32),
            "TerminateJobObject": ([HANDLE, DWORD], ctypes.c_int32),
            "TerminateProcess": ([HANDLE, DWORD], ctypes.c_int32),
            "CloseHandle": ([HANDLE], ctypes.c_int32),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.job = self.process = self.thread = None
        self.assigned = False
        self.returncode = None

    @staticmethod
    def _check(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def start(self, arguments, cwd):
        # Anonymous, non-inheritable handle. Neither BREAKAWAY_OK flag is set.
        self.job = self.api.CreateJobObjectW(None, None)
        self._check(self.job)
        limits = _ExtendedLimitInfo()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        self._check(self.api.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        startup, process = _StartupInfo(), _ProcessInfo()
        startup.cb = ctypes.sizeof(startup)
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
        # CREATE_SUSPENDED prevents the loader racing assignment by spawning Setup.
        # CREATE_NO_WINDOW; no inherited handles or shell; explicit executable path.
        self._check(self.api.CreateProcessW(arguments[0], command, None, None, False,
                    0x00000004 | 0x08000000, None, str(cwd), ctypes.byref(startup), ctypes.byref(process)))
        self.process, self.thread = process.hProcess, process.hThread
        self._check(self.api.AssignProcessToJobObject(self.job, self.process))
        self.assigned = True
        previous_count = self.api.ResumeThread(self.thread)
        if previous_count == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if previous_count != 1:
            raise OSError("Unexpected installer suspension state")
        self.api.CloseHandle(self.thread)
        self.thread = None

    def poll_parent(self):
        if self.process:
            result = self.api.WaitForSingleObject(self.process, 0)
            if result == 0:  # WAIT_OBJECT_0; retain exit code, release our reference.
                code = DWORD()
                self._check(self.api.GetExitCodeProcess(self.process, ctypes.byref(code)))
                self.returncode = code.value
                self.api.CloseHandle(self.process)
                self.process = None
            elif result != 258:  # WAIT_TIMEOUT
                raise ctypes.WinError(ctypes.get_last_error())
        return self.returncode

    def empty(self):
        accounting = _AccountingInfo()
        self._check(self.api.QueryInformationJobObject(self.job, 1, ctypes.byref(accounting),
                                                     ctypes.sizeof(accounting), None))
        return accounting.ActiveProcesses == 0

    def terminate(self):
        # Terminate is asynchronous. Its return value is never treated as proof.
        if self.assigned:
            self.api.TerminateJobObject(self.job, 1)
        elif self.process:
            # Assignment failed: this process is still suspended and cannot have
            # spawned children. Terminate and wait for this exact process handle.
            self.api.TerminateProcess(self.process, 1)

    def stopped(self):
        self.poll_parent()
        if self.assigned:
            return self.empty()
        return self.process is None

    def close(self):
        # KILL_ON_JOB_CLOSE is a final crash/error backstop, not shutdown proof.
        for name in ("thread", "process", "job"):
            handle = getattr(self, name)
            if handle:
                self.api.CloseHandle(handle)
                setattr(self, name, None)


def _stop_and_confirm(job, timeout, clock, sleep):
    deadline = clock() + timeout
    try:
        job.terminate()
    except Exception:
        pass
    while True:
        try:
            if job.stopped():
                return True
        except Exception:
            pass  # A failed query cannot establish that no writers remain.
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(0.05, remaining))


def run_installer(arguments, *, cwd, timeout=900, cleanup_timeout=30,
                  clock=time.monotonic, sleep=time.sleep):
    """Return only after the entire job exits; errors report proven shutdown.

    Installer output goes to its /LOG file, so no inherited pipes can keep this
    coordinator stuck after timeout. A failed cleanup leaves rollback forbidden.
    """
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0
               for value in (timeout, cleanup_timeout)):
        raise ValueError("Installer deadlines must be finite and positive")
    arguments = [os.fspath(value) for value in arguments]
    if not arguments or not Path(arguments[0]).is_absolute():
        raise ValueError("Installer executable must be an absolute path")
    job = _WindowsJob()
    try:
        job.start(arguments, cwd)
        deadline = clock() + timeout
        while True:
            returncode = job.poll_parent()
            if job.empty():
                # The parent can exit between the preceding poll and job query.
                if returncode is None:
                    returncode = job.poll_parent()
                if returncode is None:
                    raise OSError("Empty installer job has no parent exit status")
                return subprocess.CompletedProcess(arguments, returncode)
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError("Installer process tree exceeded its deadline")
            sleep(min(0.05, remaining))
    except BaseException as error:
        if not _stop_and_confirm(job, cleanup_timeout, clock, sleep):
            raise InstallerProcessError("Installer shutdown could not be confirmed; rollback is unsafe") from error
        if not isinstance(error, Exception):
            raise
        kind = InstallerTimeoutError if isinstance(error, TimeoutError) else InstallerProcessError
        raise kind(str(error), shutdown_confirmed=True) from error
    finally:
        job.close()
