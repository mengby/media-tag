"""A model process exits if its lease-owning local executor dies unexpectedly."""

import os
import threading

from .contracts import ModelFailure


def watch_parent(parent_pid):
    if type(parent_pid) is not int or parent_pid <= 0 or parent_pid == os.getpid():
        raise ModelFailure('invalid_executor_process')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE only, no injection/control rights.
        if not handle:
            raise ModelFailure('executor_process_unavailable')
        def wait():
            result = kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
            kernel.CloseHandle(handle)
            # A dead executor cannot complete a result. Exit even on a failed process wait.
            os._exit(3 if result == 0 else 4)
    else:
        def wait():
            event = threading.Event()
            while os.getppid() == parent_pid:
                event.wait(.1)
            os._exit(3)
    threading.Thread(target=wait, name='media-tag-executor-watch', daemon=True).start()
